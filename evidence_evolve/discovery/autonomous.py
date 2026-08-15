from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import Field

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.backends.codex_cli import CodexCliBackend, CodexRole
from evidence_evolve.budgets import BudgetLedger
from evidence_evolve.discovery.campaign import (
    CampaignCandidate,
    CandidateExecutionFailure,
    CampaignGenerationResult,
    CampaignRunner,
    EvaluationRun,
)
from evidence_evolve.discovery.director import (
    ResearchAction,
    ResearchDirector,
    ResearchDirectorDecision,
    research_action_for_mutation,
)
from evidence_evolve.discovery.population import (
    DuplicateCandidateCodeError,
    PopulationStore,
)
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import dump_contract, load_contract
from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.meta_evolution.policy import (
    DiscoveryMode,
    PolicyEffectTrace,
    ResearchPolicyGenome,
    mutation_schedule,
)
from evidence_evolve.models import MutationType, ResearchContract, ResearchStage, StrictModel
from evidence_evolve.research_memory import MemoryRole, RoleScopedMemoryPacket
from evidence_evolve.research_actions.intelligence import (
    LiteratureRepoIntelligenceExecutor,
)
from evidence_evolve.research_actions.models import (
    ActionRunResult,
    ActionState,
    ResearchActionJob,
)
from evidence_evolve.research_actions.store import ResearchActionRunner
from evidence_evolve.worktrees import WorktreeManager


class CodexExecutionBackend(Protocol):
    def run(
        self,
        *,
        role: CodexRole,
        prompt: str,
        workdir: Path,
        output_schema: Path,
        output_path: Path,
        events_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> dict[str, object]: ...


class ImplementationManifest(StrictModel):
    status: Literal["IMPLEMENTED", "NO_CHANGE", "BLOCKED"]
    summary: str = Field(min_length=1)
    tests: list[str] = Field(default_factory=list)


_CODEX_UNSUPPORTED_SCHEMA_KEYS = {
    "contains",
    "default",
    "maxContains",
    "maxLength",
    "maxProperties",
    "minContains",
    "minLength",
    "minProperties",
    "title",
    "uniqueItems",
}


def _codex_output_schema(schema: dict[str, object]) -> dict[str, object]:
    """Return the strict JSON Schema subset accepted by ``codex exec``.

    Pydantic leaves defaulted fields out of ``required`` and represents free-form
    mappings with typed ``additionalProperties``. Structured Outputs requires every
    declared property and forbids unspecified object keys, so free-form mappings are
    intentionally narrowed to empty objects at this external generation boundary.
    The validated domain models still apply their normal defaults after generation.
    """
    normalized = copy.deepcopy(schema)

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return

        for key in _CODEX_UNSUPPORTED_SCHEMA_KEYS:
            node.pop(key, None)
        if node.get("type") == "object":
            properties = node.get("properties")
            if not isinstance(properties, dict):
                properties = {}
                node["properties"] = properties
            node["required"] = list(properties)
            node["additionalProperties"] = False

        for value in node.values():
            visit(value)

    visit(normalized)
    return normalized


@dataclass(frozen=True)
class AutonomousEvaluationContext:
    generation_id: str
    candidate: CampaignCandidate
    contract: ResearchContract
    repo_root: Path
    worktree: Path
    run_dir: Path
    genetic_parent_id: str
    genetic_parent_commit: str


AutonomousEvaluationAdapter = Callable[[AutonomousEvaluationContext], EvaluationRun]


class AutonomousCampaignResult(StrictModel):
    campaign_id: str
    policy_id: str
    generations: list[CampaignGenerationResult]
    policy_effect_traces: list[PolicyEffectTrace]
    budgets: dict[str, dict[str, int]]
    population: dict[str, list[dict[str, object]]] = Field(default_factory=dict)


class AutonomousCampaignRunner:
    """Run a bounded proposal -> implementation -> evaluation feedback loop.

    Codex supplies creative proposals and candidate-only edits. The existing
    CampaignRunner remains the sole gate and receipt authority.
    """

    def __init__(
        self,
        *,
        contract: ResearchContract,
        closure_registry: ClosureRegistry,
        policy: ResearchPolicyGenome,
        repo_root: Path,
        run_dir: Path,
        evaluate: AutonomousEvaluationAdapter,
        backend: CodexExecutionBackend | None = None,
        worktree_root: Path | None = None,
        reference_metrics: dict[str, float] | None = None,
        intelligence_executor: LiteratureRepoIntelligenceExecutor | None = None,
        memory_enabled: bool = True,
        timeout_seconds: int = 1800,
    ) -> None:
        if contract.lock is None:
            raise ValueError("autonomous campaign requires a locked research contract")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.contract = contract
        self.closure_registry = closure_registry
        self.policy = policy
        self.repo_root = repo_root.resolve()
        self.run_dir = run_dir.resolve()
        self.memory_enabled = memory_enabled
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._bind_run_contract()
        self.evaluate = evaluate
        self.backend = backend or CodexCliBackend()
        self.worktrees = WorktreeManager(self.repo_root, worktree_root)
        self.reference_metrics = dict(reference_metrics or {})
        self.timeout_seconds = timeout_seconds
        self._parent_commits = {"SEED": self.contract.campaign.base_commit}
        self.database = self.run_dir / "research.db"
        self.budgets = BudgetLedger(self.database, contract.budgets)
        self.archive = ArchiveStore(self.database)
        self.action_runner = ResearchActionRunner(
            database=self.database,
            run_dir=self.run_dir,
            budgets=self.budgets,
        )
        self.intelligence_executor = intelligence_executor
        self.director = ResearchDirector()
        self.population = PopulationStore(self.database)
        self._parent_commits.update(self.population.parent_commits())
        self.campaign = CampaignRunner(
            contract=contract,
            closure_registry=closure_registry,
            policy=policy,
            run_dir=self.run_dir,
        )

    def _bind_run_contract(self) -> None:
        if self.contract.lock is None:  # pragma: no cover - guarded above
            raise ValueError("autonomous campaign requires a locked research contract")
        contract_path = self.run_dir / "contract.locked.yaml"
        if contract_path.exists():
            bound = load_contract(contract_path)
            if bound != self.contract:
                raise ValueError("run directory is bound to a different contract")
        else:
            dump_contract(self.contract, contract_path)

        manifest = {
            "campaign_id": self.contract.campaign.id,
            "contract_sha256": self.contract.lock.content_sha256,
            "base_commit": self.contract.campaign.base_commit,
            "claim_scope": self.contract.campaign.claim_scope,
            "policy_id": self.policy.policy_id,
            "policy_sha256": hashlib.sha256(
                self.policy.model_dump_json().encode("utf-8")
            ).hexdigest(),
            "scientific_memory_enabled": self.memory_enabled,
        }
        manifest_path = self.run_dir / "run_manifest.json"
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing != manifest:
                raise ValueError("run manifest is bound to a different contract")
        else:
            create_once_json(manifest_path, manifest)

    def run(
        self,
        *,
        generations: int,
        proposals_per_generation: int = 1,
        max_evaluations_per_generation: int | None = None,
        generation_prefix: str = "GEN",
        signature_tolerance: float = 0.0,
    ) -> AutonomousCampaignResult:
        if generations <= 0:
            raise ValueError("generations must be positive")
        if proposals_per_generation <= 0:
            raise ValueError("proposals_per_generation must be positive")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", generation_prefix):
            raise ValueError("generation_prefix is not a safe identifier")

        completed: list[CampaignGenerationResult] = []
        policy_effect_traces: list[PolicyEffectTrace] = []
        stagnant_generations = 0
        for generation_index in range(1, generations + 1):
            generation_id = f"{generation_prefix}-{generation_index:03d}"
            memory_packet = self._memory_packet(
                generation_id=generation_id,
                role=MemoryRole.HYPOTHESIS_EXPLORER,
                limit=12,
            )
            director_packet = self._memory_packet(
                generation_id=generation_id,
                role=MemoryRole.RESEARCH_DIRECTOR,
                limit=16,
            )
            director_path = (
                self.run_dir
                / "generations"
                / generation_id
                / "research_director_decision.json"
            )
            research_action_result: ActionRunResult | None = None
            if director_path.exists():
                director_decision = ResearchDirectorDecision.model_validate_json(
                    director_path.read_text(encoding="utf-8")
                )
                if director_decision.generation_id != generation_id:
                    raise ValueError(
                        f"research director decision drift for {generation_id}"
                    )
            else:
                director_decision = self.director.decide(
                    generation_id=generation_id,
                    packet=director_packet,
                    stagnant_generations=stagnant_generations,
                    stagnation_threshold=self.policy.stagnation_generations,
                    default_mix=self.policy.mutation_operator_mix,
                    breakthrough_mix=self.policy.breakthrough_mutation_mix,
                )
                if (
                    director_decision.primary_action
                    is ResearchAction.SEARCH_LITERATURE
                    and self.intelligence_executor is not None
                ):
                    research_action_result = self.action_runner.run(
                        ResearchActionJob(
                            action_id=f"{generation_id}-SEARCH-LITERATURE",
                            campaign_id=self.contract.campaign.id,
                            generation_id=generation_id,
                            action=ResearchAction.SEARCH_LITERATURE,
                            query=self.contract.campaign.research_question,
                            max_papers=self.policy.literature_papers_per_action,
                            max_repositories=self.policy.repositories_per_action,
                            max_source_files_per_repository=(
                                self.policy.source_files_per_repository
                            ),
                        ),
                        self.intelligence_executor,
                    )
                    if (
                        research_action_result.state
                        is ActionState.WAITING_FOR_AUTHORITY
                    ):
                        raise RuntimeError(
                            "research intelligence is waiting for authority: "
                            f"{research_action_result.reason}"
                        )
                    if research_action_result.receipt is not None:
                        memory_packet = self._memory_packet(
                            generation_id=generation_id,
                            role=MemoryRole.HYPOTHESIS_EXPLORER,
                            limit=12,
                        )
                        director_packet = self._memory_packet(
                            generation_id=generation_id,
                            role=MemoryRole.RESEARCH_DIRECTOR,
                            limit=16,
                        )
                        director_decision = self.director.decide(
                            generation_id=generation_id,
                            packet=director_packet,
                            stagnant_generations=stagnant_generations,
                            stagnation_threshold=self.policy.stagnation_generations,
                            default_mix=self.policy.mutation_operator_mix,
                            breakthrough_mix=self.policy.breakthrough_mutation_mix,
                        )
                create_once_json(director_path, director_decision)
            feedback = self._feedback_context(
                completed,
                memory_packet=memory_packet,
                director_decision=director_decision,
                research_action_result=research_action_result,
            )
            trace_path = (
                self.run_dir
                / "generations"
                / generation_id
                / "policy_effect_trace.json"
            )
            if trace_path.exists():
                trace = PolicyEffectTrace.model_validate_json(
                    trace_path.read_text(encoding="utf-8")
                )
                if trace.policy_id != self.policy.policy_id:
                    raise ValueError(
                        f"policy effect trace drift for generation {generation_id}"
                    )
                expected_candidate_ids = {
                    f"{generation_id}-C{slot:02d}"
                    for slot in range(1, proposals_per_generation + 1)
                }
                if set(trace.mutation_assignments) != expected_candidate_ids:
                    raise ValueError(
                        f"proposal count drift for generation {generation_id}"
                    )
            else:
                mode = (
                    DiscoveryMode.BREAKTHROUGH
                    if director_decision.executable_action
                    is ResearchAction.BREAKTHROUGH
                    else DiscoveryMode.NORMAL
                )
                moonshot_count = (
                    0
                    if mode is DiscoveryMode.BREAKTHROUGH
                    else int(proposals_per_generation * self.policy.moonshot_fraction)
                )
                normal_count = proposals_per_generation - moonshot_count
                assignments = mutation_schedule(
                    (
                        self.policy.breakthrough_mutation_mix
                        if mode is DiscoveryMode.BREAKTHROUGH
                        else director_decision.recommended_mutation_mix
                    ),
                    count=normal_count,
                    offset=(generation_index - 1) * proposals_per_generation,
                )
                assignments.extend(
                    mutation_schedule(
                        self.policy.breakthrough_mutation_mix,
                        count=moonshot_count,
                        offset=(generation_index - 1) * max(moonshot_count, 1),
                    )
                )
                moonshot_candidate_ids = [
                    f"{generation_id}-C{slot:02d}"
                    for slot in range(normal_count + 1, proposals_per_generation + 1)
                ]
                migrations = self.population.migrate(
                    generation_id=generation_id,
                    generation_index=generation_index,
                    island_ids=self.policy.island_ids,
                    migration_interval=self.policy.migration_interval,
                    migration_count=self.policy.migration_count,
                    island_capacity=self.policy.island_capacity,
                )
                island_assignments = {
                    f"{generation_id}-C{slot:02d}": self.policy.island_ids[
                        (
                            (generation_index - 1) * proposals_per_generation
                            + slot
                            - 1
                        )
                        % len(self.policy.island_ids)
                    ]
                    for slot in range(1, proposals_per_generation + 1)
                }
                parent_pools_by_island: dict[str, list[str]] = {}
                parent_roles: dict[str, list[str]] = {}
                for island in sorted(set(island_assignments.values())):
                    sampled = self.population.sample_parents(
                        island, self.policy.parents_per_island
                    )
                    parent_pools_by_island[island] = (
                        [item.candidate_id for item in sampled] if sampled else ["SEED"]
                    )
                    for item in sampled:
                        parent_roles[item.candidate_id] = [
                            role.value for role in item.roles
                        ]
                eligible_parent_ids = sorted(
                    {
                        parent
                        for pool in parent_pools_by_island.values()
                        for parent in pool
                    }
                )
                trace = PolicyEffectTrace(
                    generation_id=generation_id,
                    policy_id=self.policy.policy_id,
                    mode=mode,
                    reasons=(
                        [
                            "STAGNATION_THRESHOLD_REACHED",
                            self.policy.stagnation_response,
                        ]
                        if mode is DiscoveryMode.BREAKTHROUGH
                        else ["NORMAL_SEARCH"]
                    ),
                    eligible_parent_ids=eligible_parent_ids,
                    mutation_assignments={
                        f"{generation_id}-C{slot:02d}": mutation
                        for slot, mutation in enumerate(assignments, start=1)
                    },
                    moonshot_candidate_ids=moonshot_candidate_ids,
                    parent_selector=self.policy.parent_selector,
                    context_compiler=self.policy.context_compiler,
                    island_assignments=island_assignments,
                    parent_pools_by_island=parent_pools_by_island,
                    parent_roles=parent_roles,
                    migrations=[
                        item.model_dump(mode="json") for item in migrations
                    ],
                    max_parallel_proposals=self.policy.max_parallel_proposals,
                    max_parallel_evaluations=self.policy.max_parallel_evaluations,
                )
                create_once_json(trace_path, trace)
            policy_effect_traces.append(trace)

            def propose(slot: int) -> CampaignCandidate | CandidateExecutionFailure:
                candidate_id = f"{generation_id}-C{slot:02d}"
                island = trace.island_assignments.get(
                    candidate_id, self.policy.island_ids[0]
                )
                eligible_parents = trace.parent_pools_by_island.get(
                    island, trace.eligible_parent_ids
                )
                required_mutation = trace.mutation_assignments[candidate_id]
                proposal_mode = (
                    DiscoveryMode.BREAKTHROUGH
                    if candidate_id in trace.moonshot_candidate_ids
                    else trace.mode
                )
                try:
                    return self._propose_candidate(
                        generation_id=generation_id,
                        slot=slot,
                        island=island,
                        eligible_parents=eligible_parents,
                        feedback=feedback,
                        required_mutation=required_mutation,
                        research_action=research_action_for_mutation(required_mutation),
                        mode=proposal_mode,
                    )
                except Exception as exc:
                    return CandidateExecutionFailure(
                        candidate_id=candidate_id,
                        phase="PROPOSAL",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )

            slots = list(range(1, proposals_per_generation + 1))
            self._proposal_workspace()
            if self.policy.max_parallel_proposals == 1 or len(slots) <= 1:
                proposed = [propose(slot) for slot in slots]
            else:
                with ThreadPoolExecutor(
                    max_workers=self.policy.max_parallel_proposals
                ) as executor:
                    proposed = list(executor.map(propose, slots))
            candidates = [
                item for item in proposed if isinstance(item, CampaignCandidate)
            ]
            proposal_failures = [
                item
                for item in proposed
                if isinstance(item, CandidateExecutionFailure)
            ]

            if candidates:
                result = self.campaign.run_generation(
                    generation_id=generation_id,
                    candidates=candidates,
                    evaluate=lambda item, generation_id=generation_id: (
                        self._implement_and_evaluate(generation_id, item)
                    ),
                    max_evaluations=max_evaluations_per_generation,
                    max_workers=self.policy.max_parallel_evaluations,
                    signature_tolerance=signature_tolerance,
                )
                if proposal_failures:
                    result = result.model_copy(
                        update={"failures": proposal_failures + result.failures}
                    )
            else:
                result = CampaignGenerationResult(
                    generation_id=generation_id,
                    policy_id=self.policy.policy_id,
                    decisions=[],
                    failures=proposal_failures,
                )
            completed.append(result)
            candidates_by_id = {
                item.acquisition.candidate.candidate_id: item for item in candidates
            }
            score_by_id = {
                item.candidate_id: item.acquisition_score for item in result.decisions
            }
            for evaluation in result.evaluations:
                if evaluation.candidate_commit:
                    self._parent_commits[evaluation.candidate_id] = (
                        evaluation.candidate_commit
                    )
                item = candidates_by_id[evaluation.candidate_id]
                if evaluation.candidate_commit and evaluation.code_sha256:
                    self.population.admit(
                        candidate=item.acquisition.candidate,
                        generation_id=generation_id,
                        candidate_commit=evaluation.candidate_commit,
                        code_sha256=evaluation.code_sha256,
                        search_disposition=evaluation.search_disposition,
                        scientific_outcome=evaluation.verdict.scientific_outcome,
                        acquisition_score=score_by_id.get(evaluation.candidate_id),
                        information_gain=item.acquisition.signals.information_gain,
                        novelty=item.acquisition.signals.novelty,
                        parent_dispositions=set(self.policy.code_parent_dispositions),
                        stepping_stone_min_information_gain=(
                            self.policy.stepping_stone_min_information_gain
                        ),
                        island_capacity=self.policy.island_capacity,
                    )
            if any(
                item.search_disposition.value == "CODE_PARENT"
                for item in result.evaluations
            ):
                stagnant_generations = 0
            else:
                stagnant_generations += 1

        return AutonomousCampaignResult(
            campaign_id=self.contract.campaign.id,
            policy_id=self.policy.policy_id,
            generations=completed,
            policy_effect_traces=policy_effect_traces,
            budgets=self.budgets.snapshot(),
            population=self.population.snapshot(),
        )

    def _memory_packet(
        self,
        *,
        generation_id: str,
        role: MemoryRole,
        limit: int,
    ) -> RoleScopedMemoryPacket:
        if self.memory_enabled:
            return self.archive.research_memory_packet(
                role=role,
                query=self.contract.campaign.research_question,
                campaign=self.contract.campaign.id,
                limit=limit,
            )
        return RoleScopedMemoryPacket(
            retrieval_event_id=(
                f"MEMORY-DISABLED:{self.contract.campaign.id}:"
                f"{generation_id}:{role.value}"
            ),
            role=role,
            query=self.contract.campaign.research_question,
            cards=[],
        )

    def _propose_candidate(
        self,
        *,
        generation_id: str,
        slot: int,
        island: str,
        eligible_parents: list[str],
        feedback: dict[str, object],
        required_mutation: MutationType,
        research_action: ResearchAction,
        mode: DiscoveryMode,
    ) -> CampaignCandidate:
        candidate_id = f"{generation_id}-C{slot:02d}"
        generation_dir = self.run_dir / "generations" / generation_id
        proposal_path = generation_dir / "proposals" / f"{candidate_id}.json"
        if proposal_path.exists():
            candidate = CampaignCandidate.model_validate_json(
                proposal_path.read_text(encoding="utf-8")
            )
            self._validate_proposal_identity(
                candidate,
                candidate_id,
                island,
                eligible_parents,
                required_mutation,
            )
            self._validate_proposal_vocabulary(candidate)
            return candidate.model_copy(
                update={"reference_metrics": dict(self.reference_metrics)}
            )

        reservation_key = f"proposal_calls:{generation_id}:{candidate_id}"
        self.budgets.reserve("proposal_calls", 1, reservation_key)
        schema_path = generation_dir / "schemas" / f"{candidate_id}.schema.json"
        schema = self._proposal_schema(
            candidate_id,
            island,
            eligible_parents,
            required_mutation,
        )
        atomic_write_json(schema_path, schema)

        raw_path = generation_dir / "raw" / f"{candidate_id}.json"
        events_path = generation_dir / "logs" / f"{candidate_id}.proposal.events.jsonl"
        stderr_path = generation_dir / "logs" / f"{candidate_id}.proposal.stderr.log"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = self._proposal_prompt(
            candidate_id=candidate_id,
            generation_id=generation_id,
            island=island,
            eligible_parents=eligible_parents,
            feedback=feedback,
            required_mutation=required_mutation,
            research_action=research_action,
            mode=mode,
        )
        result = self.backend.run(
            role=CodexRole("hypothesis_explorer"),
            prompt=prompt,
            workdir=self._proposal_workspace(),
            output_schema=schema_path,
            output_path=raw_path,
            events_path=events_path,
            stderr_path=stderr_path,
            timeout_seconds=self.timeout_seconds,
        )
        if result.get("status") != "PASS":
            raise RuntimeError(
                f"Codex proposal failed for {candidate_id}: {result.get('status')}"
            )
        if not raw_path.is_file():
            raise RuntimeError(f"Codex proposal output missing for {candidate_id}")
        candidate = CampaignCandidate.model_validate_json(
            raw_path.read_text(encoding="utf-8")
        )
        self._validate_proposal_identity(
            candidate,
            candidate_id,
            island,
            eligible_parents,
            required_mutation,
        )
        self._validate_proposal_vocabulary(candidate)
        if candidate.reference_metrics:
            raise ValueError("Codex proposals cannot supply reference metrics")
        declared = audit_candidate(
            self.contract,
            candidate.acquisition.candidate,
            ClosureRegistry(),
            changed_files=[],
        )
        declared_scope_violations = [
            issue for issue in declared.violations if issue.startswith("DECLARED_")
        ]
        if declared_scope_violations:
            raise ValueError(
                "Codex proposal declared invalid editable scope: "
                + ",".join(declared_scope_violations)
            )
        normalized = candidate.model_copy(
            update={"reference_metrics": dict(self.reference_metrics)}
        )
        create_once_json(proposal_path, normalized)
        raw_path.unlink(missing_ok=True)
        return normalized

    def _implement_and_evaluate(
        self, generation_id: str, item: CampaignCandidate
    ) -> EvaluationRun:
        candidate = item.acquisition.candidate
        genetic_parent_id = candidate.genetic_parent_id or candidate.parent_ids[0]
        genetic_parent_commit = self._parent_commits.get(genetic_parent_id)
        if genetic_parent_commit is None:
            raise ValueError(
                f"genetic parent has no evaluated code artifact: {genetic_parent_id}"
            )
        candidate_dir = self.run_dir / "candidates" / candidate.candidate_id
        implementation_path = candidate_dir / "implementation.json"
        run_hash = hashlib.sha256(str(self.run_dir).encode("utf-8")).hexdigest()[:8]
        worktree_key = (
            f"{self.contract.campaign.id}-{run_hash}-{candidate.candidate_id}"
        )
        worktree = self.worktrees.candidate_path(worktree_key)
        if not worktree.exists():
            worktree = self.worktrees.create(
                worktree_key, genetic_parent_commit
            )
        self._assert_worktree_descends_from_base(worktree)
        self._assert_worktree_contains_parent(worktree, genetic_parent_commit)

        if implementation_path.exists():
            manifest = ImplementationManifest.model_validate_json(
                implementation_path.read_text(encoding="utf-8")
            )
        else:
            self.budgets.reserve(
                "implementations",
                1,
                f"implementations:{generation_id}:{candidate.candidate_id}",
            )
            schema_path = candidate_dir / "implementation.schema.json"
            atomic_write_json(schema_path, self._implementation_schema())
            raw_path = candidate_dir / "implementation.raw.json"
            result = self.backend.run(
                role=CodexRole("implementer", writable=True),
                prompt=self._implementation_prompt(item),
                workdir=worktree,
                output_schema=schema_path,
                output_path=raw_path,
                events_path=candidate_dir / "logs" / "implementation.events.jsonl",
                stderr_path=candidate_dir / "logs" / "implementation.stderr.log",
                timeout_seconds=self.timeout_seconds,
            )
            if result.get("status") != "PASS":
                raise RuntimeError(
                    "Codex implementation failed for "
                    f"{candidate.candidate_id}: {result.get('status')}"
                )
            if not raw_path.is_file():
                raise RuntimeError(
                    f"Codex implementation output missing for {candidate.candidate_id}"
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
        candidate_commit = self._commit_valid_candidate_changes(
            worktree,
            item,
            genetic_parent_commit,
        )
        if candidate_commit == genetic_parent_commit:
            raise ValueError(
                f"candidate {candidate.candidate_id} produced no parent-relative code "
                "change; no candidate effect can be attributed"
            )
        candidate_ref = self.worktrees.pin_commit(
            run_hash,
            candidate.candidate_id,
            candidate_commit,
        )
        baseline_patch = self._diff_bytes(
            worktree,
            self.contract.campaign.base_commit,
            candidate_commit,
        )
        parent_patch = self._diff_bytes(
            worktree,
            genetic_parent_commit,
            candidate_commit,
        )
        code_sha256 = sha256_bytes(baseline_patch)
        duplicate_of = self.population.claim_code(
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
        run = self.evaluate(
            AutonomousEvaluationContext(
                generation_id=generation_id,
                candidate=item,
                contract=self.contract,
                repo_root=self.repo_root,
                worktree=worktree,
                run_dir=self.run_dir,
                genetic_parent_id=genetic_parent_id,
                genetic_parent_commit=genetic_parent_commit,
            )
        )
        return run.model_copy(
            update={
                "genetic_parent_id": genetic_parent_id,
                "genetic_parent_commit": genetic_parent_commit,
                "candidate_commit": candidate_commit,
                "candidate_ref": candidate_ref,
                "patch_sha256": code_sha256,
                "parent_patch_sha256": sha256_bytes(parent_patch),
            }
        )

    def _proposal_schema(
        self,
        candidate_id: str,
        island: str,
        eligible_parents: list[str],
        required_mutation: MutationType,
    ) -> dict[str, object]:
        schema = copy.deepcopy(CampaignCandidate.model_json_schema())
        definitions = schema.get("$defs")
        if not isinstance(definitions, dict):
            raise RuntimeError("CampaignCandidate schema is missing definitions")
        genome = definitions.get("CandidateGenome")
        if not isinstance(genome, dict):
            raise RuntimeError("CampaignCandidate schema is missing CandidateGenome")
        properties = genome.get("properties")
        if not isinstance(properties, dict):
            raise RuntimeError("CandidateGenome schema is missing properties")
        properties["candidate_id"] = {
            "const": candidate_id,
            "title": "Candidate Id",
            "type": "string",
        }
        properties["island"] = {"const": island, "type": "string"}
        parent_ids = properties.get("parent_ids")
        if isinstance(parent_ids, dict):
            parent_ids["items"] = {"enum": eligible_parents, "type": "string"}
        properties["genetic_parent_id"] = {
            "enum": eligible_parents,
            "type": "string",
        }
        properties["mutation_type"] = {
            "const": required_mutation.value,
            "type": "string",
        }
        properties["required_controls"] = {
            "items": {
                "enum": sorted(self.contract.required_controls),
                "type": "string",
            },
            "maxItems": len(self.contract.required_controls),
            "minItems": len(self.contract.required_controls),
            "type": "array",
        }
        expected_signature = definitions.get("ExpectedSignature")
        if not isinstance(expected_signature, dict):
            raise RuntimeError("CampaignCandidate schema is missing ExpectedSignature")
        signature_properties = expected_signature.get("properties")
        if not isinstance(signature_properties, dict):
            raise RuntimeError("ExpectedSignature schema is missing properties")
        metric_ids = sorted(self.contract.metrics.pareto_objectives)
        for field in ("improve", "unchanged"):
            signature = signature_properties.get(field)
            if isinstance(signature, dict):
                signature["items"] = {"enum": metric_ids, "type": "string"}
        root_properties = schema.get("properties")
        if isinstance(root_properties, dict):
            root_properties["stage"] = {
                "const": ResearchStage.M0_MECHANICS.value,
                "default": ResearchStage.M0_MECHANICS.value,
                "title": "Stage",
                "type": "string",
            }
            references = root_properties.get("reference_metrics")
            if isinstance(references, dict):
                references["maxProperties"] = 0
        acquisition = definitions.get("CandidateAcquisition")
        if isinstance(acquisition, dict):
            acquisition_properties = acquisition.get("properties")
            if isinstance(acquisition_properties, dict):
                verified = acquisition_properties.get("verified_reopen_conditions")
                if isinstance(verified, dict):
                    verified["maxItems"] = 0
        return _codex_output_schema(schema)

    @staticmethod
    def _implementation_schema() -> dict[str, object]:
        return _codex_output_schema(ImplementationManifest.model_json_schema())

    @staticmethod
    def _validate_proposal_identity(
        candidate: CampaignCandidate,
        expected_id: str,
        expected_island: str,
        eligible_parents: list[str],
        required_mutation: MutationType,
    ) -> None:
        genome = candidate.acquisition.candidate
        if genome.candidate_id != expected_id:
            raise ValueError(
                f"proposal candidate id mismatch: expected={expected_id} "
                f"actual={genome.candidate_id}"
            )
        if genome.island != expected_island:
            raise ValueError(
                f"proposal {expected_id} ignored island assignment: "
                f"expected={expected_island} actual={genome.island}"
            )
        if not set(genome.parent_ids) & set(eligible_parents):
            raise ValueError(
                f"proposal {expected_id} does not descend from an evaluated parent"
            )
        if genome.genetic_parent_id not in eligible_parents:
            raise ValueError(
                f"proposal {expected_id} has an ineligible genetic parent: "
                f"{genome.genetic_parent_id}"
            )
        if genome.mutation_type is not required_mutation:
            raise ValueError(
                f"proposal {expected_id} ignored policy mutation assignment: "
                f"expected={required_mutation.value} actual={genome.mutation_type.value}"
            )
        if candidate.stage is not ResearchStage.M0_MECHANICS:
            raise ValueError("autonomous discovery proposals must start at M0_MECHANICS")
        if candidate.acquisition.verified_reopen_conditions:
            raise ValueError(
                "Codex proposals cannot supply verified reopen conditions"
            )

    def _validate_proposal_vocabulary(self, candidate: CampaignCandidate) -> None:
        genome = candidate.acquisition.candidate
        frozen_controls = set(self.contract.required_controls)
        declared_controls = set(genome.required_controls)
        if (
            declared_controls != frozen_controls
            or len(genome.required_controls) != len(frozen_controls)
        ):
            raise ValueError(
                "proposal required controls do not match the frozen contract: "
                f"expected={self.contract.required_controls} "
                f"actual={genome.required_controls}"
            )
        frozen_metrics = set(self.contract.metrics.pareto_objectives)
        declared_metrics = set(genome.expected_signature.improve) | set(
            genome.expected_signature.unchanged
        )
        unknown_metrics = sorted(declared_metrics - frozen_metrics)
        if unknown_metrics:
            raise ValueError(
                "proposal expected signature uses metrics not frozen as Pareto "
                f"objectives: {unknown_metrics}"
            )

    def _proposal_prompt(
        self,
        *,
        candidate_id: str,
        generation_id: str,
        island: str,
        eligible_parents: list[str],
        feedback: dict[str, object],
        required_mutation: MutationType,
        research_action: ResearchAction,
        mode: DiscoveryMode,
    ) -> str:
        contract_context = {
            "campaign": self.contract.campaign.model_dump(mode="json"),
            "editable_scope": self.contract.editable_scope.model_dump(mode="json"),
            "metrics": self.contract.metrics.model_dump(mode="json"),
            "required_controls": self.contract.required_controls,
            "eligible_development_sources": [
                {
                    "source_id": source.source_id,
                    "grade": source.grade.value,
                    "permissions": sorted(item.value for item in source.permissions),
                }
                for source in self.contract.evidence_sources
                if any(item.value == "DEV" for item in source.permissions)
            ],
            "closures": self.closure_registry.model_dump(mode="json"),
        }
        return (
            self._prompt_template("explorer.md")
            + "\nYou are the EvidenceEvolve Hypothesis Explorer. Return exactly one "
            "CampaignCandidate matching the supplied JSON Schema. Your values are "
            "proposal and scheduling inputs only; do not claim scientific authority.\n"
            f"Required candidate_id: {candidate_id}\n"
            f"Generation: {generation_id}\n"
            f"Required island: {island}\n"
            f"Use at least one parent_id from: {json.dumps(eligible_parents)}\n"
            f"Set genetic_parent_id to the one parent whose code is inherited.\n"
            f"Required mutation_type: {required_mutation.value}\n"
            f"Required research action: {research_action.value}\n"
            f"Discovery mode: {mode.value}\n"
            "Set reference_metrics to an empty object; the frozen harness replaces it. "
            "Set verified_reopen_conditions to an empty array. Do not access confirmation "
            "assets or claim reopen evidence. Propose one "
            "falsifiable mechanism change inside editable_scope.allow and list exact "
            "editable_files and a falsifier. Copy required_controls exactly from "
            "the frozen context. Use only frozen pareto_objective IDs in both "
            "expected_signature lists. In BREAKTHROUGH mode, avoid local parameter-only "
            "changes and propose a structural, representational, or cross-family jump.\n"
            "Frozen context:\n"
            + json.dumps(contract_context, ensure_ascii=False, sort_keys=True)
            + "\nPrior evaluated feedback (SCHEDULING_ONLY where marked):\n"
            + json.dumps(feedback, ensure_ascii=False, sort_keys=True)
        )

    def _implementation_prompt(self, item: CampaignCandidate) -> str:
        return (
            self._prompt_template("implementer.md")
            + "\nYou are the EvidenceEvolve Implementer. Implement only the approved "
            "Candidate Genome in this dedicated Git worktree. Edit only files listed "
            "in candidate.editable_files and allowed by editable_scope. Never read or "
            "modify confirmation assets, evaluators, protocols, gates, budgets, or "
            "research policy. Do not commit. Run only narrow mechanics checks useful "
            "for the candidate. Return the required ImplementationManifest; make no "
            "scientific or verdict claim.\nCandidate:\n"
            + item.acquisition.candidate.model_dump_json(indent=2)
            + "\nEditable scope:\n"
            + self.contract.editable_scope.model_dump_json(indent=2)
        )

    def _prompt_template(self, name: str) -> str:
        path = self.repo_root / "prompts" / name
        if not path.is_file():
            raise FileNotFoundError(f"required Codex prompt is missing: {path}")
        return path.read_text(encoding="utf-8").strip()

    def _feedback_context(
        self,
        completed: list[CampaignGenerationResult],
        *,
        memory_packet: RoleScopedMemoryPacket,
        director_decision: ResearchDirectorDecision,
        research_action_result: ActionRunResult | None,
    ) -> dict[str, object]:
        return {
            "archive_summary": self.archive.summary(),
            "research_memory": memory_packet.model_dump(mode="json"),
            "research_director": director_decision.model_dump(mode="json"),
            "research_action_result": (
                research_action_result.model_dump(mode="json")
                if research_action_result is not None
                else None
            ),
            "completed_generations": [
                {
                    "generation_id": item.generation_id,
                    "evaluations": [
                        {
                            "candidate_id": evaluation.candidate_id,
                            "scientific_outcome": (
                                evaluation.verdict.scientific_outcome.value
                            ),
                            "gate_decision": evaluation.verdict.decision.value,
                            "search_disposition": evaluation.search_disposition.value,
                            "claim_ceiling": evaluation.claim_ceiling.value,
                            "mechanism_support": (
                                evaluation.mechanism.support.value
                                if evaluation.mechanism is not None
                                else None
                            ),
                            "mechanism_reasons": (
                                evaluation.mechanism.reasons
                                if evaluation.mechanism is not None
                                else []
                            ),
                        }
                        for evaluation in item.evaluations
                    ],
                    "failures": [
                        {
                            "candidate_id": failure.candidate_id,
                            "phase": failure.phase,
                            "error_type": failure.error_type,
                            "error": failure.error[:400],
                        }
                        for failure in item.failures
                    ],
                }
                for item in completed
            ],
        }

    def _proposal_workspace(self) -> Path:
        digest = hashlib.sha256(str(self.run_dir).encode("utf-8")).hexdigest()[:16]
        workspace = (
            Path(tempfile.gettempdir())
            / "evidence-evolve-proposal-contexts"
            / digest
        )
        workspace.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
        return workspace

    def _assert_worktree_descends_from_base(self, worktree: Path) -> None:
        completed = subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                self.contract.campaign.base_commit,
                "HEAD",
            ],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"candidate worktree does not descend from frozen base: {worktree}"
            )

    @staticmethod
    def _assert_worktree_contains_parent(worktree: Path, parent_commit: str) -> None:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", parent_commit, "HEAD"],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"candidate worktree does not inherit genetic parent {parent_commit}"
            )

    @staticmethod
    def _diff_bytes(worktree: Path, base_commit: str, head_commit: str) -> bytes:
        return subprocess.run(
            ["git", "diff", "--binary", base_commit, head_commit, "--"],
            cwd=worktree,
            check=True,
            capture_output=True,
        ).stdout

    def _commit_valid_candidate_changes(
        self,
        worktree: Path,
        item: CampaignCandidate,
        genetic_parent_commit: str,
    ) -> str:
        changed_files = self.worktrees.changed_files(
            worktree, genetic_parent_commit
        )
        inherited_scope_violations = self.worktrees.audit(
            worktree,
            self.contract.campaign.base_commit,
            self.contract.editable_scope,
        )
        if inherited_scope_violations:
            raise ValueError(
                "candidate lineage violates frozen editable scope: "
                + ",".join(inherited_scope_violations)
            )
        audit = audit_candidate(
            self.contract,
            item.acquisition.candidate,
            self.closure_registry,
            changed_files=changed_files,
            verified_reopen_conditions=item.acquisition.verified_reopen_conditions,
        )
        if not audit.valid:
            raise ValueError(
                "candidate implementation audit failed: " + ",".join(audit.violations)
            )
        if not changed_files:
            return subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        subprocess.run(["git", "add", "--all"], cwd=worktree, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=worktree,
            check=False,
        )
        if staged.returncode == 0:
            return subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        if staged.returncode != 1:
            raise RuntimeError("failed to inspect staged candidate changes")
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=EvidenceEvolve",
                "-c",
                "user.email=evidence-evolve@local.invalid",
                "commit",
                "-m",
                f"candidate: {item.acquisition.candidate.candidate_id}",
            ],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
        candidate_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self._assert_worktree_contains_parent(worktree, genetic_parent_commit)
        return candidate_commit
