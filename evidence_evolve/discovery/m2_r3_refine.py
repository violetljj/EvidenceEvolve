"""M2-R3 basin-local refinement and scheduling-only lineage feedback.

This module is additive: it does not alter the frozen Set Cover evaluator or any
completed M2 campaign.  Profiles produced here have no gate or scientific
authority; they exist only to give later proposals structured, within-basin
credit-assignment context.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.discovery.m2_escape import M2AutonomousCampaignRunner, M2EscapePolicy
from evidence_evolve.discovery.throughput import (
    CandidateTicket,
    FunnelDecision,
    FunnelStage,
    StageStatus,
)
from evidence_evolve.governance.protocol_lock import load_contract
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode
from evidence_evolve.models import (
    EvidencePermission,
    MechanicsStatus,
    MutationType,
    ScientificOutcome,
    StrictModel,
)
from tasks.algotune_set_cover.common import (
    _fresh,
    _load_solver,
    _valid_solution,
    generate_problem,
    reference_solution,
    solve_reference,
)
from tasks.algotune_set_cover.staged_adapter import SetCoverStagedAdapter


class BasinSeed(StrictModel):
    basin_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    root_candidate_id: str = Field(min_length=2)
    family: str = Field(min_length=2)
    root_score: float = Field(gt=0.0)
    source_run: str = Field(min_length=1)
    source_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_markers: list[str] = Field(min_length=1)
    claim_term_groups: list[list[str]] = Field(min_length=1)
    primary_endpoint: bool = False

    @model_validator(mode="after")
    def terms_are_nonempty(self) -> "BasinSeed":
        if any(not group or any(not term.strip() for term in group) for group in self.claim_term_groups):
            raise ValueError("basin claim term groups must be non-empty")
        if any(not marker.strip() for marker in self.source_markers):
            raise ValueError("basin source markers must be non-empty")
        return self


class M2R3Policy(M2EscapePolicy):
    operator_class: Literal["BASIN_REFINE"] = "BASIN_REFINE"
    incumbent_metric: Literal["raw_speedup"] = "raw_speedup"
    global_incumbent: float = Field(gt=0.0)
    conversion_threshold: float = Field(gt=0.0)
    initial_waves: Literal[3] = 3
    slots_per_initial_wave: Literal[4] = 4
    adaptive_slots: Literal[4] = 4
    total_proposal_slots: Literal[16] = 16
    max_adaptive_slots_per_basin: Literal[2] = 2
    early_stop_non_improving_attempts: Literal[3] = 3
    improvement_min_delta: float = Field(default=0.1, gt=0.0)
    probe_parent_fraction: float = Field(default=0.5, gt=0.0, le=1.0)
    profile_repeats: Literal[3] = 3
    profile_workers_per_candidate: int = Field(default=4, ge=1, le=16)
    profile_parallel_candidates: int = Field(default=4, ge=1, le=8)
    hybrid_permitted: Literal[False] = False
    structural_escape_permitted: Literal[False] = False
    basins: list[BasinSeed] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def pilot_is_bounded_and_has_one_primary_endpoint(self) -> "M2R3Policy":
        if self.initial_waves * self.slots_per_initial_wave + self.adaptive_slots != (
            self.total_proposal_slots
        ):
            raise ValueError("M2-R3 waves must exactly allocate the frozen slot budget")
        basin_ids = [item.basin_id for item in self.basins]
        root_ids = [item.root_candidate_id for item in self.basins]
        if len(set(basin_ids)) != len(basin_ids) or len(set(root_ids)) != len(root_ids):
            raise ValueError("M2-R3 basin and root identities must be unique")
        primary = [item for item in self.basins if item.primary_endpoint]
        if len(primary) != 1:
            raise ValueError("M2-R3 requires exactly one primary endpoint basin")
        if not (primary[0].root_score < self.conversion_threshold < self.global_incumbent):
            raise ValueError("conversion threshold must close part of the primary basin gap")
        active_normal = {
            mutation
            for mutation, weight in self.mutation_operator_mix.items()
            if weight > 0.0
        }
        if active_normal != {MutationType.MECHANISM}:
            raise ValueError("M2-R3 isolates the mechanism BASIN_REFINE mutation")
        return self


class BasinAttempt(StrictModel):
    candidate_id: str
    parent_candidate_id: str
    wave_id: str
    score: float | None = Field(default=None, ge=0.0)
    dev_valid: bool
    basin_retained: bool
    improved_local_incumbent: bool
    terminal_stage: str
    terminal_status: str
    failure_type: str | None = None
    failure: str | None = None


class BasinState(StrictModel):
    basin_id: str
    root_candidate_id: str
    local_incumbent_id: str
    root_score: float = Field(gt=0.0)
    local_incumbent_score: float = Field(gt=0.0)
    attempts: list[BasinAttempt] = Field(default_factory=list)
    consecutive_non_improving: int = Field(default=0, ge=0)

    @property
    def improvement_slope(self) -> float:
        if not self.attempts:
            return 0.0
        return (self.local_incumbent_score - self.root_score) / len(self.attempts)


class InstanceTiming(StrictModel):
    seed: int
    valid: bool
    candidate_time_ns: int = Field(ge=0)
    reference_time_ns: int = Field(ge=0)
    speedup: float = Field(ge=0.0)
    failure_type: str | None = None


class BasinRuntimeProfile(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    instances: list[InstanceTiming] = Field(min_length=1)
    valid_instances: int = Field(ge=0)
    aggregate_speedup: float = Field(ge=0.0)
    candidate_time_p50_ns: int = Field(ge=0)
    candidate_time_p90_ns: int = Field(ge=0)
    candidate_time_p99_ns: int = Field(ge=0)
    worker_count: int = Field(ge=1)
    wall_seconds: float = Field(ge=0.0)
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )
    blind_artifacts_read: Literal[False] = False
    confirmation_runs: Literal[0] = 0


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return int(ordered[index])


def _profile_chunk(
    candidate_path: str,
    seeds: list[int],
    repeats: int,
    problem_size: int,
) -> list[dict[str, object]]:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    try:
        solver = _load_solver(Path(candidate_path))
    except Exception as exc:  # pragma: no cover - exercised through profile result
        return [
            {
                "seed": seed,
                "valid": False,
                "candidate_time_ns": 0,
                "reference_time_ns": 0,
                "speedup": 0.0,
                "failure_type": f"LOAD_{type(exc).__name__.upper()}",
            }
            for seed in seeds
        ]
    rows: list[dict[str, object]] = []
    for seed in seeds:
        problem = tuple(tuple(item) for item in generate_problem(problem_size, seed))
        reference = reference_solution(problem)
        try:
            proposed = solver.solve(_fresh(problem))
            if not _valid_solution(problem, proposed, len(reference)):
                raise ValueError("invalid warmup solution")
            candidate_times: list[int] = []
            reference_times: list[int] = []
            for _ in range(repeats):
                before = time.perf_counter_ns()
                solve_reference(problem)
                reference_times.append(time.perf_counter_ns() - before)
                before = time.perf_counter_ns()
                timed = solver.solve(_fresh(problem))
                candidate_times.append(time.perf_counter_ns() - before)
                if not _valid_solution(problem, timed, len(reference)):
                    raise ValueError("invalid timed solution")
            candidate_ns = min(candidate_times)
            reference_ns = min(reference_times)
            rows.append(
                {
                    "seed": seed,
                    "valid": True,
                    "candidate_time_ns": candidate_ns,
                    "reference_time_ns": reference_ns,
                    "speedup": reference_ns / candidate_ns if candidate_ns else 0.0,
                    "failure_type": None,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "seed": seed,
                    "valid": False,
                    "candidate_time_ns": 0,
                    "reference_time_ns": 0,
                    "speedup": 0.0,
                    "failure_type": type(exc).__name__.upper(),
                }
            )
    return rows


def profile_commit(
    *,
    repo: Path,
    candidate_id: str,
    candidate_commit: str,
    seeds: list[int],
    repeats: int,
    workers: int,
    problem_size: int = 52,
) -> BasinRuntimeProfile:
    """Profile one commit without granting the result evaluator authority."""

    started = time.perf_counter()
    source = subprocess.run(
        ["git", "show", f"{candidate_commit}:tasks/algotune_set_cover/initial.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    worker_count = min(max(workers, 1), len(seeds))
    with tempfile.TemporaryDirectory(prefix="ee-m2-r3-profile-") as temp_dir:
        candidate_path = Path(temp_dir) / "candidate.py"
        candidate_path.write_bytes(source)
        chunks = [seeds[index::worker_count] for index in range(worker_count)]
        if worker_count == 1:
            raw_rows = _profile_chunk(
                str(candidate_path), chunks[0], repeats, problem_size
            )
        else:
            with ProcessPoolExecutor(max_workers=worker_count) as pool:
                futures = [
                    pool.submit(
                        _profile_chunk,
                        str(candidate_path),
                        chunk,
                        repeats,
                        problem_size,
                    )
                    for chunk in chunks
                ]
                raw_rows = [row for future in futures for row in future.result()]
    instances = [InstanceTiming.model_validate(row) for row in raw_rows]
    instances.sort(key=lambda item: item.seed)
    valid = [item for item in instances if item.valid]
    candidate_total = sum(item.candidate_time_ns for item in valid)
    reference_total = sum(item.reference_time_ns for item in valid)
    candidate_times = [item.candidate_time_ns for item in valid]
    return BasinRuntimeProfile(
        candidate_id=candidate_id,
        candidate_commit=candidate_commit,
        instances=instances,
        valid_instances=len(valid),
        aggregate_speedup=(
            reference_total / candidate_total if candidate_total and len(valid) == len(seeds) else 0.0
        ),
        candidate_time_p50_ns=_percentile(candidate_times, 0.50),
        candidate_time_p90_ns=_percentile(candidate_times, 0.90),
        candidate_time_p99_ns=_percentile(candidate_times, 0.99),
        worker_count=worker_count,
        wall_seconds=time.perf_counter() - started,
    )


def compare_profiles(
    parent: BasinRuntimeProfile,
    child: BasinRuntimeProfile,
) -> dict[str, object]:
    parent_rows = {item.seed: item for item in parent.instances}
    child_rows = {item.seed: item for item in child.instances}
    shared = sorted(set(parent_rows) & set(child_rows))
    wins: list[tuple[float, int]] = []
    losses: list[tuple[float, int]] = []
    ties: list[int] = []
    invalid: list[int] = []
    for seed in shared:
        before = parent_rows[seed]
        after = child_rows[seed]
        if not before.valid or not after.valid or after.candidate_time_ns <= 0:
            invalid.append(seed)
            continue
        ratio = before.candidate_time_ns / after.candidate_time_ns
        if ratio > 1.0:
            wins.append((ratio, seed))
        elif ratio < 1.0:
            losses.append((ratio, seed))
        else:
            ties.append(seed)
    wins.sort(reverse=True)
    losses.sort()
    return {
        "parent_candidate_id": parent.candidate_id,
        "child_candidate_id": child.candidate_id,
        "win_count": len(wins),
        "loss_count": len(losses),
        "tie_count": len(ties),
        "invalid_or_unshared_count": len(invalid),
        "winning_seed_ids": [seed for _ratio, seed in wins],
        "losing_seed_ids": [seed for _ratio, seed in losses],
        "largest_wins": [
            {"seed": seed, "parent_over_child_time_ratio": ratio}
            for ratio, seed in wins[:10]
        ],
        "largest_losses": [
            {"seed": seed, "parent_over_child_time_ratio": ratio}
            for ratio, seed in losses[:10]
        ],
        "parent_runtime_ns": {
            "p50": parent.candidate_time_p50_ns,
            "p90": parent.candidate_time_p90_ns,
            "p99": parent.candidate_time_p99_ns,
        },
        "child_runtime_ns": {
            "p50": child.candidate_time_p50_ns,
            "p90": child.candidate_time_p90_ns,
            "p99": child.candidate_time_p99_ns,
        },
        "scientific_authority": "NONE_SCHEDULING_ONLY",
    }


def allocate_adaptive_slots(
    states: dict[str, BasinState],
    policy: M2R3Policy,
) -> list[str]:
    """Freeze deterministic slope allocation without retries or replacement slots."""

    eligible = [
        state
        for state in states.values()
        if state.consecutive_non_improving
        < policy.early_stop_non_improving_attempts
    ]
    eligible.sort(
        key=lambda item: (
            -item.improvement_slope,
            -item.local_incumbent_score,
            item.basin_id,
        )
    )
    allocation: list[str] = []
    counts = {item.basin_id: 0 for item in eligible}
    while eligible and len(allocation) < policy.adaptive_slots:
        added = False
        for state in eligible:
            if counts[state.basin_id] >= policy.max_adaptive_slots_per_basin:
                continue
            allocation.append(state.basin_id)
            counts[state.basin_id] += 1
            added = True
            if len(allocation) == policy.adaptive_slots:
                break
        if not added:
            break
    return allocation


class BasinRetentionAudit:
    """Conservative mechanics gate for frozen basin-specific code anchors."""

    def __init__(self, policy: M2R3Policy) -> None:
        self._markers = {
            item.basin_id: tuple(item.source_markers) for item in policy.basins
        }

    def __call__(self, ticket: CandidateTicket, candidate_path: Path) -> bool:
        markers = self._markers.get(ticket.lineage_id)
        if markers is None:
            return False
        try:
            source = candidate_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return all(marker in source for marker in markers)


class BasinRefinementStagedAdapter(SetCoverStagedAdapter):
    """Basin-local L0 identity gate and parent-relative L1 scheduling threshold."""

    def __init__(
        self,
        *,
        local_incumbents: dict[str, float],
        probe_parent_fraction: float,
        retention_audit: BasinRetentionAudit,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.local_incumbents = dict(local_incumbents)
        self.probe_parent_fraction = probe_parent_fraction
        self.retention_audit = retention_audit

    def l0(self, ticket: CandidateTicket, item: Any) -> FunnelDecision:
        if not self.retention_audit(ticket, self.candidate_path(item)):
            return FunnelDecision(
                stage=FunnelStage.L0,
                status=StageStatus.BLOCK,
                continue_pipeline=False,
                mechanics_status=MechanicsStatus.FAIL,
                data_eligible=False,
                controls={"basin_mechanism_retained": False},
                metrics={},
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reason_codes=["BASIN_MECHANISM_ESCAPE_BLOCK"],
            )
        decision = super().l0(ticket, item)
        return decision.model_copy(
            update={
                "controls": {
                    **decision.controls,
                    "basin_mechanism_retained": True,
                }
            }
        )

    def l1(
        self,
        ticket: CandidateTicket,
        item: Any,
        l0: FunnelDecision,
    ) -> FunnelDecision:
        if not l0.continue_pipeline:
            raise ValueError("L1 cannot run after an L0 block")
        raw = self._run(
            item,
            seeds=self.policy.probe_seeds,
            repeats=self.policy.probe_repeats,
        )
        valid = bool(raw.get("correct")) and not bool(raw.get("adapter_exception"))
        speedup = float(raw.get("raw_speedup", 0.0))
        threshold = (
            self.local_incumbents[ticket.lineage_id] * self.probe_parent_fraction
        )
        promoted = valid and speedup >= threshold
        reason_codes = [
            "BASIN_LOCAL_PROBE_PROMOTE" if promoted else "BASIN_LOCAL_PROBE_BLOCK"
        ]
        if failure_reason := self._failure_reason(raw):
            reason_codes.append(failure_reason)
        return FunnelDecision(
            stage=FunnelStage.L1,
            status=StageStatus.PASS if promoted else StageStatus.BLOCK,
            continue_pipeline=promoted,
            mechanics_status=(MechanicsStatus.PASS if valid else MechanicsStatus.FAIL),
            data_eligible=valid,
            controls=self._controls(raw),
            metrics={
                "raw_speedup": speedup,
                "invalid_solution_rate": 1.0 - float(raw.get("valid_rate", 0.0)),
                "probe_instance_count": float(raw.get("instance_count", 0)),
                "probe_elapsed_seconds": float(raw["elapsed_seconds"]),
                "probe_parent_relative_threshold": threshold,
                "evaluator_worker_count": float(raw.get("worker_count", 1)),
            },
            scientific_outcome=self._outcome(raw),
            reason_codes=reason_codes,
        )


class M2R3AutonomousCampaignRunner(M2AutonomousCampaignRunner):
    """Normal-mode runner that rejects cross-basin and structural escape proposals."""

    def __init__(
        self,
        *args: Any,
        r3_policy: M2R3Policy,
        context_run_dirs: list[Path],
        **kwargs: Any,
    ) -> None:
        self.r3_policy = r3_policy
        self._basins = {item.basin_id: item for item in r3_policy.basins}
        self._candidate_contracts: dict[str, dict[str, str]] = {}
        self._contract_lock = threading.Lock()
        self._context_run_dirs = tuple(path.resolve() for path in context_run_dirs)
        self._validate_context_runs()
        super().__init__(*args, **kwargs)
        if self.policy != r3_policy.frozen_base_policy():
            raise ValueError("runner base policy does not match M2-R3 policy")
        self._bind_r3_context()

    def _validate_context_runs(self) -> None:
        prohibited = {EvidencePermission.CONFIRM, EvidencePermission.CLAIM}
        for run_dir in self._context_run_dirs:
            contract = load_contract(run_dir / "contract.locked.yaml")
            if contract.lock is None or contract.budgets.confirmation_runs != 0:
                raise ValueError("M2-R3 context must be locked and development-only")
            permissions = {
                permission
                for source in contract.evidence_sources
                for permission in source.permissions
            }
            if permissions & prohibited or contract.authority.confirmation_visible_to_agents:
                raise ValueError("M2-R3 context exposes confirmation or claim authority")
            if "DEVELOPMENT_ONLY" not in contract.campaign.claim_scope:
                raise ValueError("M2-R3 context is not development-only")

    def _bind_r3_context(self) -> None:
        payload = {
            "schema_version": "1.0",
            "operator_class": self.r3_policy.operator_class,
            "policy_sha256": sha256_object(self.r3_policy.model_dump(mode="json")),
            "context_runs": [
                {
                    "path": str(path),
                    "contract_sha256": load_contract(
                        path / "contract.locked.yaml"
                    ).lock.content_sha256,
                    "manifest_sha256": sha256_file(path / "run_manifest.json"),
                }
                for path in self._context_run_dirs
            ],
            "hybrid_permitted": False,
            "structural_escape_permitted": False,
            "evidence_scope": "DEVELOPMENT_ONLY",
            "scientific_authority": "NONE_SCHEDULING_ONLY",
            "blind_artifacts_read": False,
            "confirmation_runs": 0,
        }
        path = self.run_dir / "m2_r3_context_manifest.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != payload:
                raise ValueError("M2-R3 context manifest drift")
        else:
            create_once_json(path, payload)

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
        raw_operator = feedback.get("async_operator_contract")
        if not isinstance(raw_operator, dict):
            raise ValueError("M2-R3 proposal is missing its async operator contract")
        try:
            raw = json.loads(str(raw_operator["operator_directive"]))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("M2-R3 operator directive is not a basin contract") from exc
        if not isinstance(raw, dict):
            raise ValueError("M2-R3 basin contract must be an object")
        contract = {
            "basin_id": str(raw.get("basin_id", "")),
            "parent_candidate_id": str(raw.get("parent_candidate_id", "")),
        }
        if contract["basin_id"] not in self._basins:
            raise ValueError("M2-R3 operator directive references an unknown basin")
        candidate_id = f"{generation_id}-C{slot:02d}"
        if mode is not DiscoveryMode.NORMAL:
            raise ValueError("M2-R3 forbids STRUCTURAL_ESCAPE")
        if required_mutation is not MutationType.MECHANISM:
            raise ValueError("M2-R3 requires the BASIN_REFINE mechanism operator")
        with self._contract_lock:
            self._candidate_contracts[candidate_id] = contract
        return super()._propose_candidate(
            generation_id=generation_id,
            slot=slot,
            island=island,
            eligible_parents=eligible_parents,
            feedback=feedback,
            required_mutation=required_mutation,
            research_action=research_action,
            mode=mode,
        )

    def _proposal_prompt(self, **kwargs: Any) -> str:
        prompt = super()._proposal_prompt(**kwargs)
        candidate_id = str(kwargs["candidate_id"])
        contract = self._candidate_contracts[candidate_id]
        basin = self._basins[contract["basin_id"]]
        return (
            prompt
            + "\nM2-R3 operator class is BASIN_REFINE. Improve the supplied basin's "
            "measured bottlenecks without changing solver family, crossing into another "
            "basin, adding a new primary algorithm, or hybridizing with another basin. "
            "The sole genetic and contextual parent is the basin-local incumbent. Keep "
            "the exact frozen family string and preserve the listed mechanism anchors. "
            "Use the instance wins/losses, runtime distribution, failure types, and "
            "lineage deltas to choose one falsifiable local mechanism improvement. A "
            "scalar speedup alone is insufficient credit assignment. All feedback is "
            "development-only and scheduling-only.\n"
            f"Frozen basin_id: {basin.basin_id}\n"
            f"Exact family: {basin.family}\n"
            f"Sole parent: {contract['parent_candidate_id']}\n"
            f"Required source anchors: {json.dumps(basin.source_markers)}\n"
            f"Required mechanism term groups: {json.dumps(basin.claim_term_groups)}"
        )

    def _validate_proposal_vocabulary(self, candidate: CampaignCandidate) -> None:
        super()._validate_proposal_vocabulary(candidate)
        genome = candidate.acquisition.candidate
        contract = self._candidate_contracts.get(genome.candidate_id)
        if contract is None:
            raise ValueError("M2-R3 candidate has no frozen basin contract")
        basin = self._basins.get(contract["basin_id"])
        if basin is None:
            raise ValueError("M2-R3 candidate references an unknown basin")
        parent = contract["parent_candidate_id"]
        if genome.genetic_parent_id != parent or genome.parent_ids != [parent]:
            raise ValueError("M2-R3 forbids cross-basin parents and crossover context")
        if genome.family.strip() != basin.family:
            raise ValueError("M2-R3 refinement changed the frozen basin family")
        if genome.mutation_type is not MutationType.MECHANISM:
            raise ValueError("M2-R3 proposal is not a BASIN_REFINE mechanism mutation")
        text = " ".join(
            [
                genome.hypothesis,
                genome.intervention,
                *genome.mechanism_claims,
                *genome.transfer_motifs,
            ]
        ).casefold()
        for group in basin.claim_term_groups:
            if not any(term.casefold() in text for term in group):
                raise ValueError(
                    f"M2-R3 proposal omitted basin mechanism terms: {group}"
                )


__all__ = [
    "BasinAttempt",
    "BasinRefinementStagedAdapter",
    "BasinRetentionAudit",
    "BasinRuntimeProfile",
    "BasinSeed",
    "BasinState",
    "M2R3AutonomousCampaignRunner",
    "M2R3Policy",
    "allocate_adaptive_slots",
    "compare_profiles",
    "profile_commit",
]
