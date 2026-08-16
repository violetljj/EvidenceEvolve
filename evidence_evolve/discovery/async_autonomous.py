"""Candidate-level asynchronous wave execution for autonomous discovery."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.backends.codex_cli import CodexRole
from evidence_evolve.discovery.autonomous import (
    AutonomousCampaignRunner,
    ImplementationManifest,
)
from evidence_evolve.discovery.campaign import (
    CampaignCandidate,
    CandidateRunResult,
    EvaluationRun,
)
from evidence_evolve.discovery.director import research_action_for_mutation
from evidence_evolve.discovery.population import DuplicateCandidateCodeError
from evidence_evolve.discovery.throughput import (
    AsyncFunnelEngine,
    CandidateFunnelRecord,
    CandidateTicket,
    FunnelCallbacks,
    FunnelDecision,
    FunnelStage,
    StageStatus,
    ThroughputPolicy,
    ThroughputRunResult,
)
from evidence_evolve.hashing import sha256_bytes, sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode, rank_candidates
from evidence_evolve.models import (
    MechanicsStatus,
    MutationType,
    ScientificOutcome,
    StrictModel,
)


class StagedDevelopmentAdapter(Protocol):
    def l0(
        self, ticket: CandidateTicket, item: "MaterializedCandidate"
    ) -> FunnelDecision: ...

    def l1(
        self,
        ticket: CandidateTicket,
        item: "MaterializedCandidate",
        l0: FunnelDecision,
    ) -> FunnelDecision: ...

    def full_evaluation(self, item: "MaterializedCandidate") -> EvaluationRun: ...

    def promotion_worthy(self, evaluation: EvaluationRun) -> bool: ...

    def structural_transition_pass(
        self, ticket: CandidateTicket, item: "MaterializedCandidate"
    ) -> bool: ...

    def structural_root_key(
        self, ticket: CandidateTicket, item: "MaterializedCandidate"
    ) -> str | None: ...


class AsyncWaveSlot(StrictModel):
    slot: int = Field(ge=1, le=99)
    dispatch_index: int = Field(ge=1)
    operator_class: str = Field(min_length=1)
    lineage_id: str = Field(min_length=1)
    island: str = Field(min_length=1)
    eligible_parent_ids: list[str] = Field(min_length=1)
    primary_parent_id: str = Field(min_length=1)
    mutation: MutationType
    mode: DiscoveryMode
    requires_structural_transition: bool = False
    operator_directive: str = Field(
        default="Follow the frozen operator class.", min_length=8
    )

    @model_validator(mode="after")
    def primary_parent_is_eligible(self) -> "AsyncWaveSlot":
        if self.primary_parent_id not in self.eligible_parent_ids:
            raise ValueError("primary parent must be inside the frozen parent pool")
        if len(set(self.eligible_parent_ids)) != len(self.eligible_parent_ids):
            raise ValueError("eligible parent ids must be unique")
        return self


class AsyncWaveSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    wave_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    slots: list[AsyncWaveSlot] = Field(min_length=1)
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    blind_artifacts_read: Literal[False] = False
    confirmation_runs: Literal[0] = 0

    @model_validator(mode="after")
    def slots_are_disjoint(self) -> "AsyncWaveSpec":
        for label, values in (
            ("slot", [item.slot for item in self.slots]),
            ("dispatch index", [item.dispatch_index for item in self.slots]),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"async wave has a duplicate {label}")
        return self

    def candidate_id(self, slot: AsyncWaveSlot) -> str:
        return f"{self.wave_id}-C{slot.slot:02d}"


class AsyncAutonomousWaveResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    wave_id: str
    throughput: ThroughputRunResult
    receipt_paths: dict[str, str]
    blind_artifacts_read: Literal[False] = False
    confirmation_runs: Literal[0] = 0


@dataclass(frozen=True)
class MaterializedCandidate:
    generation_id: str
    item: CampaignCandidate
    worktree: Path
    changed_files: list[str]
    genetic_parent_id: str
    genetic_parent_commit: str
    candidate_commit: str
    candidate_ref: str
    patch_sha256: str
    parent_patch_sha256: str


class AsyncAutonomousWaveRunner:
    """Wire real proposal/materialization to the work-conserving funnel.

    The wrapped autonomous runner continues to own contracts, worktrees, budgets,
    code deduplication, gates, receipts, archive state, and population rights.
    """

    def __init__(
        self,
        *,
        runner: AutonomousCampaignRunner,
        throughput_policy: ThroughputPolicy,
        staged_adapter: StagedDevelopmentAdapter,
    ) -> None:
        self.runner = runner
        self.throughput_policy = throughput_policy
        self.staged_adapter = staged_adapter
        self._lock = threading.Lock()
        self._items: dict[str, CampaignCandidate] = {}
        self._materialized: dict[str, MaterializedCandidate] = {}
        self._candidate_results: dict[str, CandidateRunResult] = {}
        self._acquisition_scores: dict[str, float | None] = {}

    def _bind_wave(
        self,
        wave: AsyncWaveSpec,
        feedback: dict[str, object],
    ) -> None:
        staged_policy = getattr(self.staged_adapter, "policy", None)
        staged_policy_payload = (
            staged_policy.model_dump(mode="json")
            if hasattr(staged_policy, "model_dump")
            else None
        )
        payload = {
            "schema_version": "1.0",
            "wave": wave.model_dump(mode="json"),
            "throughput_policy": self.throughput_policy.model_dump(mode="json"),
            "throughput_policy_sha256": sha256_object(
                self.throughput_policy.model_dump(mode="json")
            ),
            "feedback_sha256": sha256_object(feedback),
            "contract_sha256": self.runner.contract.lock.content_sha256,
            "execution_semantics": (
                "AS_COMPLETED_STAGE_EXECUTION_SINGLE_WRITER_ADMISSION"
            ),
            "l0_l1_authority": "SCHEDULING_ONLY",
            "l2_authority": "FROZEN_CAMPAIGN_RUNNER",
            "staged_adapter_class": (
                f"{type(self.staged_adapter).__module__}."
                f"{type(self.staged_adapter).__qualname__}"
            ),
            "staged_policy": staged_policy_payload,
            "staged_policy_sha256": (
                sha256_object(staged_policy_payload)
                if staged_policy_payload is not None
                else None
            ),
            "blind_artifacts_read": False,
            "confirmation_runs": 0,
        }
        path = self.runner.run_dir / "waves" / wave.wave_id / "wave_manifest.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != payload:
                raise ValueError("async wave manifest drift")
        else:
            create_once_json(path, payload)

    def _materialize(
        self,
        generation_id: str,
        item: CampaignCandidate,
    ) -> MaterializedCandidate:
        candidate = item.acquisition.candidate
        genetic_parent_id = candidate.genetic_parent_id or candidate.parent_ids[0]
        genetic_parent_commit = self.runner._parent_commits.get(genetic_parent_id)  # noqa: SLF001
        if genetic_parent_commit is None:
            raise ValueError(
                f"genetic parent has no evaluated code artifact: {genetic_parent_id}"
            )
        candidate_dir = self.runner.run_dir / "candidates" / candidate.candidate_id
        implementation_path = candidate_dir / "implementation.json"
        run_hash = hashlib.sha256(
            str(self.runner.run_dir).encode("utf-8")
        ).hexdigest()[:8]
        worktree_key = (
            f"{self.runner.contract.campaign.id}-{run_hash}-{candidate.candidate_id}"
        )
        worktree = self.runner.worktrees.candidate_path(worktree_key)
        if not worktree.exists():
            worktree = self.runner.worktrees.create(
                worktree_key, genetic_parent_commit
            )
        self.runner._assert_worktree_descends_from_base(worktree)  # noqa: SLF001
        self.runner._assert_worktree_contains_parent(  # noqa: SLF001
            worktree, genetic_parent_commit
        )

        if implementation_path.exists():
            manifest = ImplementationManifest.model_validate_json(
                implementation_path.read_text(encoding="utf-8")
            )
        else:
            self.runner.budgets.reserve(
                "implementations",
                1,
                f"implementations:{generation_id}:{candidate.candidate_id}",
            )
            schema_path = candidate_dir / "implementation.schema.json"
            atomic_write_json(schema_path, self.runner._implementation_schema())  # noqa: SLF001
            raw_path = candidate_dir / "implementation.raw.json"
            result = self.runner.backend.run(
                role=CodexRole("implementer", writable=True),
                prompt=self.runner._implementation_prompt(item),  # noqa: SLF001
                workdir=worktree,
                output_schema=schema_path,
                output_path=raw_path,
                events_path=candidate_dir / "logs" / "implementation.events.jsonl",
                stderr_path=candidate_dir / "logs" / "implementation.stderr.log",
                timeout_seconds=self.runner.timeout_seconds,
            )
            if result.get("status") != "PASS" or not raw_path.is_file():
                raise RuntimeError(
                    f"Codex implementation failed for {candidate.candidate_id}"
                )
            manifest = ImplementationManifest.model_validate_json(
                raw_path.read_text(encoding="utf-8")
            )
            create_once_json(implementation_path, manifest)
            raw_path.unlink(missing_ok=True)
        if manifest.status == "BLOCKED":
            raise RuntimeError(
                f"Codex implementer blocked for {candidate.candidate_id}: "
                f"{manifest.summary}"
            )

        candidate_commit = self.runner._commit_valid_candidate_changes(  # noqa: SLF001
            worktree, item, genetic_parent_commit
        )
        if candidate_commit == genetic_parent_commit:
            raise ValueError(
                f"candidate {candidate.candidate_id} produced no parent-relative change"
            )
        candidate_ref = self.runner.worktrees.pin_commit(
            run_hash, candidate.candidate_id, candidate_commit
        )
        baseline_patch = self.runner._diff_bytes(  # noqa: SLF001
            worktree,
            self.runner.contract.campaign.base_commit,
            candidate_commit,
        )
        parent_patch = self.runner._diff_bytes(  # noqa: SLF001
            worktree, genetic_parent_commit, candidate_commit
        )
        code_sha256 = sha256_bytes(baseline_patch)
        duplicate_of = self.runner.population.claim_code(
            candidate_id=candidate.candidate_id,
            generation_id=generation_id,
            code_sha256=code_sha256,
        )
        if duplicate_of is not None:
            create_once_json(
                candidate_dir / "duplicate_code.json",
                {
                    "candidate_id": candidate.candidate_id,
                    "duplicate_of": duplicate_of,
                    "code_sha256": code_sha256,
                    "evaluator_executed": False,
                },
            )
            raise DuplicateCandidateCodeError(
                candidate.candidate_id, duplicate_of, code_sha256
            )
        return MaterializedCandidate(
            generation_id=generation_id,
            item=item,
            worktree=worktree,
            changed_files=self.runner.worktrees.changed_files(
                worktree, genetic_parent_commit
            ),
            genetic_parent_id=genetic_parent_id,
            genetic_parent_commit=genetic_parent_commit,
            candidate_commit=candidate_commit,
            candidate_ref=candidate_ref,
            patch_sha256=code_sha256,
            parent_patch_sha256=sha256_bytes(parent_patch),
        )

    def _l2(
        self,
        ticket: CandidateTicket,
        materialized: MaterializedCandidate,
        l1: FunnelDecision,
    ) -> FunnelDecision:
        del l1
        evaluation_run = self.staged_adapter.full_evaluation(materialized)
        generation = self.runner.campaign.run_generation(
            generation_id=materialized.generation_id,
            candidates=[materialized.item],
            evaluate=lambda _candidate: evaluation_run,
            max_evaluations=1,
            max_workers=1,
        )
        if generation.failures or len(generation.evaluations) != 1:
            detail = generation.failures[0].error if generation.failures else "missing L2 result"
            raise RuntimeError(f"L2 evaluation failed for {ticket.candidate_id}: {detail}")
        result = generation.evaluations[0]
        receipt_path = self.runner.run_dir / result.receipt_path
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))["receipt"]
        evaluation = receipt["evaluation_input"]
        controls = {
            str(key): bool(value)
            for key, value in evaluation.get("controls", {}).items()
        }
        mechanics = MechanicsStatus(str(evaluation["mechanics_status"]))
        hard_pass = bool(
            result.verdict.protocol_valid
            and mechanics is MechanicsStatus.PASS
            and evaluation["data_eligible"]
            and controls
            and all(controls.values())
        )
        acquisition_score = generation.decisions[0].acquisition_score
        with self._lock:
            self._candidate_results[ticket.candidate_id] = result
            self._acquisition_scores[ticket.candidate_id] = acquisition_score
        promotion_worthy = hard_pass and self.staged_adapter.promotion_worthy(
            evaluation_run
        )
        admission_eligible = hard_pass and (
            result.search_disposition in set(self.runner.policy.code_parent_dispositions)
        )
        structural_root_key = (
            self.staged_adapter.structural_root_key(ticket, materialized)
            if hard_pass
            else None
        )
        return FunnelDecision(
            stage=FunnelStage.L2,
            status=StageStatus.PASS if hard_pass else StageStatus.BLOCK,
            mechanics_status=mechanics,
            data_eligible=bool(evaluation["data_eligible"]),
            controls=controls,
            metrics={
                str(key): float(value)
                for key, value in evaluation.get("metrics", {}).items()
            },
            scientific_outcome=result.verdict.scientific_outcome,
            reason_codes=[result.verdict.decision.value],
            admission_eligible=admission_eligible,
            promotion_worthy=promotion_worthy,
            structural_transition_pass=(
                structural_root_key is not None
            ),
            structural_root_key=structural_root_key,
            incumbent_improved=promotion_worthy,
        )

    def _admit(self, record: CandidateFunnelRecord) -> None:
        candidate_id = record.ticket.candidate_id
        with self._lock:
            item = self._items[candidate_id]
            materialized = self._materialized[candidate_id]
            result = self._candidate_results[candidate_id]
            score = self._acquisition_scores[candidate_id]
        self.runner._parent_commits[candidate_id] = materialized.candidate_commit  # noqa: SLF001
        self.runner.population.admit(
            candidate=item.acquisition.candidate,
            generation_id=materialized.generation_id,
            candidate_commit=materialized.candidate_commit,
            code_sha256=materialized.patch_sha256,
            search_disposition=result.search_disposition,
            scientific_outcome=result.verdict.scientific_outcome,
            acquisition_score=score,
            information_gain=item.acquisition.signals.information_gain,
            novelty=item.acquisition.signals.novelty,
            parent_dispositions=set(self.runner.policy.code_parent_dispositions),
            stepping_stone_min_information_gain=(
                self.runner.policy.stepping_stone_min_information_gain
            ),
            island_capacity=self.runner.policy.island_capacity,
        )

    def run_wave(
        self,
        *,
        wave: AsyncWaveSpec,
        feedback: dict[str, object],
    ) -> AsyncAutonomousWaveResult:
        if len(wave.slots) > self.throughput_policy.total_candidate_budget:
            raise ValueError("wave slots exceed the frozen candidate budget")
        self._bind_wave(wave, feedback)
        result_path = (
            self.runner.run_dir / "waves" / wave.wave_id / "wave_result.json"
        )
        if result_path.is_file():
            return AsyncAutonomousWaveResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
        # The base runner lazily creates one shared read-only proposal context.
        # Initialize it before proposal threads start so concurrent git init calls
        # cannot race each other.
        self.runner._proposal_workspace()  # noqa: SLF001
        slots_by_candidate = {
            wave.candidate_id(slot): slot for slot in wave.slots
        }

        def propose(ticket: CandidateTicket) -> CampaignCandidate:
            slot = slots_by_candidate[ticket.candidate_id]
            slot_feedback = {
                **feedback,
                "async_operator_contract": {
                    "candidate_id": ticket.candidate_id,
                    "operator_class": slot.operator_class,
                    "operator_directive": slot.operator_directive,
                    "primary_parent_id": slot.primary_parent_id,
                },
            }
            item = self.runner._propose_candidate(  # noqa: SLF001
                generation_id=wave.wave_id,
                slot=slot.slot,
                island=slot.island,
                eligible_parents=slot.eligible_parent_ids,
                feedback=slot_feedback,
                required_mutation=slot.mutation,
                research_action=research_action_for_mutation(slot.mutation),
                mode=slot.mode,
            )
            decision = rank_candidates(
                policy=self.runner.policy,
                candidates=[item.acquisition],
                closure_registry=self.runner.closure_registry,
            )[0]
            if not decision.eligible:
                raise ValueError(
                    f"proposal {ticket.candidate_id} is ineligible: {decision.reasons}"
                )
            with self._lock:
                self._items[ticket.candidate_id] = item
            return item

        def implement(
            ticket: CandidateTicket,
            item: CampaignCandidate,
        ) -> MaterializedCandidate:
            materialized = self._materialize(wave.wave_id, item)
            with self._lock:
                self._materialized[ticket.candidate_id] = materialized
            return materialized

        callbacks = FunnelCallbacks(
            propose=propose,
            implement=implement,
            l0=self.staged_adapter.l0,
            l1=self.staged_adapter.l1,
            l2=self._l2,
            admit=self._admit,
        )
        tickets = [
            CandidateTicket(
                candidate_id=wave.candidate_id(slot),
                dispatch_index=slot.dispatch_index,
                lineage_id=slot.lineage_id,
                operator_class=slot.operator_class,
                genetic_parent_id=slot.primary_parent_id,
                requires_structural_transition=slot.requires_structural_transition,
            )
            for slot in wave.slots
        ]
        throughput = AsyncFunnelEngine(
            self.throughput_policy, callbacks
        ).run(tickets)
        result = AsyncAutonomousWaveResult(
            wave_id=wave.wave_id,
            throughput=throughput,
            receipt_paths={
                candidate_id: result.receipt_path
                for candidate_id, result in sorted(self._candidate_results.items())
            },
        )
        create_once_json(result_path, result)
        return result


__all__ = [
    "AsyncAutonomousWaveResult",
    "AsyncAutonomousWaveRunner",
    "AsyncWaveSlot",
    "AsyncWaveSpec",
    "MaterializedCandidate",
    "StagedDevelopmentAdapter",
]
