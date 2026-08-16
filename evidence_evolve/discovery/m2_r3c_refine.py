"""M2-R3C activation-gated, telemetry-conditioned basin refinement.

R3C is a new policy version.  It does not reinterpret or mutate the completed R3
campaign.  All telemetry and briefs in this module are development-only scheduling
inputs with no gate or scientific authority except the explicit mechanics canary.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.backends.codex_cli import CodexRole
from evidence_evolve.discovery.autonomous import _codex_output_schema
from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.discovery.m2_escape import M2AutonomousCampaignRunner, M2EscapePolicy
from evidence_evolve.discovery.m2_r3_refine import BasinRetentionAudit, BasinSeed
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
)
from tasks.algotune_set_cover.staged_adapter import SetCoverStagedAdapter


class M2R3CPolicy(M2EscapePolicy):
    operator_class: Literal["BASIN_REFINE_TELEMETRY"] = (
        "BASIN_REFINE_TELEMETRY"
    )
    incumbent_metric: Literal["raw_speedup"] = "raw_speedup"
    global_incumbent: float = Field(gt=0.0)
    conversion_threshold: float = Field(gt=0.0)
    candidate_slots: Literal[4] = 4
    early_stop_non_improving_attempts: Literal[3] = 3
    improvement_min_delta: float = Field(default=0.1, gt=0.0)
    probe_parent_fraction: float = Field(default=0.5, gt=0.0, le=1.0)
    minimum_activation_rate: float = Field(default=0.8, ge=0.0, le=1.0)
    maximum_fallback_rate: float = Field(default=0.2, ge=0.0, le=1.0)
    feedback_brief_max_bytes: int = Field(default=4096, ge=1024, le=8192)
    telemetry_workers: int = Field(default=4, ge=1, le=16)
    telemetry_seed_count: Literal[100] = 100
    exactness_exhaustive_universe_size: Literal[4] = 4
    exactness_exhaustive_max_family_size: Literal[5] = 5
    hybrid_permitted: Literal[False] = False
    structural_escape_permitted: Literal[False] = False
    basins: list[BasinSeed] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def isolates_one_active_refinement_basin(self) -> "M2R3CPolicy":
        active = {
            mutation
            for mutation, weight in self.mutation_operator_mix.items()
            if weight > 0.0
        }
        if active != {MutationType.MECHANISM}:
            raise ValueError("M2-R3C isolates the mechanism BASIN_REFINE operator")
        if not self.basins[0].primary_endpoint:
            raise ValueError("M2-R3C requires its sole basin to be the primary endpoint")
        if not (
            self.basins[0].root_score
            < self.conversion_threshold
            < self.global_incumbent
        ):
            raise ValueError("R3C conversion threshold must close part of the active gap")
        return self


class ActivationInstance(StrictModel):
    seed: int
    valid: bool
    optimal_cardinality: int = Field(ge=0)
    result_cardinality: int = Field(ge=0)
    kernel_search_calls: int = Field(ge=0)
    kernel_search_successes: int = Field(ge=0)
    fallback_calls: int = Field(ge=0)
    propagation_calls: int = Field(ge=0)
    forced_events: int = Field(ge=0)
    failure_type: str | None = None


class BasinActivationProfile(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    instances: list[ActivationInstance] = Field(min_length=1)
    activation_rate: float = Field(ge=0.0, le=1.0)
    primary_completion_rate: float = Field(ge=0.0, le=1.0)
    fallback_rate: float = Field(ge=0.0, le=1.0)
    invalid_rate: float = Field(ge=0.0, le=1.0)
    mean_kernel_search_calls: float = Field(ge=0.0)
    mean_kernel_search_successes: float = Field(ge=0.0)
    mean_propagation_calls: float = Field(ge=0.0)
    mean_forced_events: float = Field(ge=0.0)
    worker_count: int = Field(ge=1)
    wall_seconds: float = Field(ge=0.0)
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )
    blind_artifacts_read: Literal[False] = False
    confirmation_runs: Literal[0] = 0


class ExactnessCanaryResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    passed: bool
    cases_executed: int = Field(ge=0)
    failed_case_id: str | None = None
    expected_cardinality: int | None = Field(default=None, ge=0)
    actual_cardinality: int | None = Field(default=None, ge=0)
    failure_type: str | None = None
    synthetic_only: Literal[True] = True
    scientific_authority: Literal["MECHANICS_ONLY"] = "MECHANICS_ONLY"


class StageFeedback(StrictModel):
    stage: str
    status: str
    mechanics_status: MechanicsStatus | None = None
    data_eligible: bool
    controls: dict[str, bool]
    metrics: dict[str, float]
    scientific_outcome: ScientificOutcome | None = None
    reason_codes: list[str]


class AttemptFeedback(StrictModel):
    candidate_id: str
    parent_candidate_id: str
    candidate_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    stages: list[StageFeedback]
    terminal_stage: str
    terminal_status: str
    error_type: str | None = None
    error: str | None = None
    local_incumbent_improved: bool = False


class BasinRefinementBrief(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    brief_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    candidate_id: str
    basin_id: str
    parent_candidate_id: str
    parent_score: float = Field(gt=0.0)
    available_counters: dict[str, float] = Field(min_length=1)
    regression_seed_ids: list[int] = Field(min_length=1, max_length=10)
    improvement_seed_ids: list[int] = Field(max_length=10)
    parent_runtime_ns: dict[str, int]
    activation_gate: dict[str, float | bool]
    last_attempt: AttemptFeedback | None = None
    last_parent_child_delta: dict[str, object] | None = None
    required_plan_fields: list[str] = Field(min_length=5)
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )

    def assert_size(self, maximum_bytes: int) -> None:
        size = len(self.model_dump_json().encode("utf-8"))
        if size > maximum_bytes:
            raise ValueError(
                f"R3C feedback brief exceeds frozen size: {size}>{maximum_bytes}"
            )


class BasinRefinementPlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    basin_id: str
    parent_candidate_id: str
    brief_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    observed_counter: str = Field(min_length=2)
    observed_value: float
    target_counter: str = Field(min_length=2)
    expected_direction: Literal["INCREASE", "DECREASE", "UNCHANGED"]
    expected_delta: float
    addressed_seed_ids: list[int] = Field(min_length=1, max_length=10)
    bottleneck_evidence: str = Field(min_length=12)
    mechanism_change: str = Field(min_length=12)
    correctness_invariants: list[str] = Field(min_length=1)
    falsifier: str = Field(min_length=12)
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )


def _trace_activation_chunk(
    candidate_path: str,
    seeds: list[int],
) -> list[dict[str, object]]:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    try:
        solver = _load_solver(Path(candidate_path))
    except Exception as exc:
        return [
            {
                "seed": seed,
                "valid": False,
                "optimal_cardinality": 0,
                "result_cardinality": 0,
                "kernel_search_calls": 0,
                "kernel_search_successes": 0,
                "fallback_calls": 0,
                "propagation_calls": 0,
                "forced_events": 0,
                "failure_type": f"LOAD_{type(exc).__name__.upper()}",
            }
            for seed in seeds
        ]
    rows: list[dict[str, object]] = []
    for seed in seeds:
        problem = tuple(tuple(item) for item in generate_problem(52, seed))
        optimum = reference_solution(problem)
        counters = {
            "kernel_search_calls": 0,
            "kernel_search_successes": 0,
            "fallback_calls": 0,
            "propagation_calls": 0,
            "forced_events": 0,
        }

        def trace(frame: Any, event: str, arg: Any) -> Any:
            name = frame.f_code.co_name
            if event == "call":
                if name == "kernel_search":
                    counters["kernel_search_calls"] += 1
                elif name == "inherited_solver":
                    counters["fallback_calls"] += 1
                elif name == "propagate_forced_pivots":
                    counters["propagation_calls"] += 1
            elif event == "return" and name == "propagate_forced_pivots":
                if isinstance(arg, tuple) and len(arg) >= 3 and isinstance(arg[2], int):
                    counters["forced_events"] += max(0, arg[2])
            elif event == "return" and name == "kernel_search" and arg is True:
                counters["kernel_search_successes"] += 1
            return trace

        try:
            sys.settrace(trace)
            result = solver.solve(_fresh(problem))
        except Exception as exc:
            result = []
            failure = type(exc).__name__.upper()
        else:
            failure = None
        finally:
            sys.settrace(None)
        valid = _valid_solution(problem, result, len(optimum))
        rows.append(
            {
                "seed": seed,
                "valid": valid,
                "optimal_cardinality": len(optimum),
                "result_cardinality": len(result) if isinstance(result, (list, tuple)) else 0,
                **counters,
                "failure_type": failure if failure else (None if valid else "INVALID_SOLUTION"),
            }
        )
    return rows


def activation_profile_commit(
    *,
    repo: Path,
    candidate_id: str,
    candidate_commit: str,
    seeds: list[int],
    workers: int,
) -> BasinActivationProfile:
    started = time.perf_counter()
    source = subprocess.run(
        ["git", "show", f"{candidate_commit}:tasks/algotune_set_cover/initial.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    worker_count = min(max(workers, 1), len(seeds))
    with tempfile.TemporaryDirectory(prefix="ee-m2-r3c-activation-") as temp_dir:
        path = Path(temp_dir) / "candidate.py"
        path.write_bytes(source)
        chunks = [seeds[index::worker_count] for index in range(worker_count)]
        if worker_count == 1:
            raw_rows = _trace_activation_chunk(str(path), chunks[0])
        else:
            with ProcessPoolExecutor(max_workers=worker_count) as pool:
                futures = [
                    pool.submit(_trace_activation_chunk, str(path), chunk)
                    for chunk in chunks
                ]
                raw_rows = [row for future in futures for row in future.result()]
    instances = [ActivationInstance.model_validate(row) for row in raw_rows]
    instances.sort(key=lambda item: item.seed)
    count = len(instances)
    active = sum(item.kernel_search_successes > 0 for item in instances)
    primary = sum(
        item.kernel_search_successes > 0 and item.fallback_calls == 0 and item.valid
        for item in instances
    )
    fallback = sum(item.fallback_calls > 0 for item in instances)
    invalid = sum(not item.valid for item in instances)
    return BasinActivationProfile(
        candidate_id=candidate_id,
        candidate_commit=candidate_commit,
        instances=instances,
        activation_rate=active / count,
        primary_completion_rate=primary / count,
        fallback_rate=fallback / count,
        invalid_rate=invalid / count,
        mean_kernel_search_calls=sum(item.kernel_search_calls for item in instances) / count,
        mean_kernel_search_successes=(
            sum(item.kernel_search_successes for item in instances) / count
        ),
        mean_propagation_calls=sum(item.propagation_calls for item in instances) / count,
        mean_forced_events=sum(item.forced_events for item in instances) / count,
        worker_count=worker_count,
        wall_seconds=time.perf_counter() - started,
    )


def _optimal_cardinality(problem: list[list[int]]) -> int:
    universe = {item for subset in problem for item in subset}
    for count in range(len(problem) + 1):
        for indices in itertools.combinations(range(len(problem)), count):
            covered = {
                item for index in indices for item in problem[index]
            }
            if covered == universe:
                return count
    return 0


def _exactness_cases() -> list[tuple[str, list[list[int]], int | None]]:
    subsets = [
        [item for item in range(1, 5) if mask & (1 << (item - 1))]
        for mask in range(1, 1 << 4)
    ]
    full = {1, 2, 3, 4}
    cases: list[tuple[str, list[list[int]], int | None]] = []
    for size in range(1, 6):
        for index, family in enumerate(itertools.combinations(subsets, size)):
            problem = [list(item) for item in family]
            if {item for subset in problem for item in subset} == full:
                cases.append((f"EXHAUSTIVE_N4_K{size}_{index}", problem, None))
    regression = [list(item) for item in generate_problem(52, 57)]
    regression_frozen = tuple(tuple(item) for item in regression)
    cases.append(
        (
            "REGRESSION_R3_PIVOT_EXACTNESS_DEV_SEED_57",
            regression,
            len(reference_solution(regression_frozen)),
        )
    )
    return cases


def run_exactness_canary(candidate_path: Path) -> ExactnessCanaryResult:
    try:
        solver = _load_solver(candidate_path)
    except Exception as exc:
        return ExactnessCanaryResult(
            passed=False,
            cases_executed=0,
            failed_case_id="LOAD",
            failure_type=type(exc).__name__.upper(),
        )
    executed = 0
    for case_id, problem, frozen_expected in _exactness_cases():
        expected = (
            frozen_expected
            if frozen_expected is not None
            else _optimal_cardinality(problem)
        )
        try:
            result = solver.solve([list(item) for item in problem])
        except Exception as exc:
            return ExactnessCanaryResult(
                passed=False,
                cases_executed=executed + 1,
                failed_case_id=case_id,
                expected_cardinality=expected,
                actual_cardinality=0,
                failure_type=type(exc).__name__.upper(),
            )
        executed += 1
        frozen = tuple(tuple(item) for item in problem)
        if not _valid_solution(frozen, result, expected):
            return ExactnessCanaryResult(
                passed=False,
                cases_executed=executed,
                failed_case_id=case_id,
                expected_cardinality=expected,
                actual_cardinality=(
                    len(result) if isinstance(result, (list, tuple)) else 0
                ),
                failure_type="INVALID_OR_NONOPTIMAL_SOLUTION",
            )
    return ExactnessCanaryResult(passed=True, cases_executed=executed)


class BasinRefinementCStagedAdapter(SetCoverStagedAdapter):
    """R3C L0 combines basin retention, exactness regression, and mechanics."""

    def __init__(
        self,
        *,
        local_incumbents: dict[str, float],
        probe_parent_fraction: float,
        retention_audit: BasinRetentionAudit,
        canary_dir: Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.local_incumbents = dict(local_incumbents)
        self.probe_parent_fraction = probe_parent_fraction
        self.retention_audit = retention_audit
        self.canary_dir = canary_dir

    def l0(self, ticket: CandidateTicket, item: Any) -> FunnelDecision:
        path = self.candidate_path(item)
        if not self.retention_audit(ticket, path):
            return FunnelDecision(
                stage=FunnelStage.L0,
                status=StageStatus.BLOCK,
                mechanics_status=MechanicsStatus.FAIL,
                data_eligible=False,
                controls={"basin_mechanism_retained": False},
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reason_codes=["BASIN_MECHANISM_ESCAPE_BLOCK"],
            )
        canary = run_exactness_canary(path)
        canary_path = self.canary_dir / f"{ticket.candidate_id}.json"
        if canary_path.exists():
            frozen = ExactnessCanaryResult.model_validate_json(
                canary_path.read_text(encoding="utf-8")
            )
            if frozen != canary:
                raise ValueError(f"R3C exactness canary drift: {ticket.candidate_id}")
        else:
            create_once_json(canary_path, canary)
        if not canary.passed:
            return FunnelDecision(
                stage=FunnelStage.L0,
                status=StageStatus.BLOCK,
                mechanics_status=MechanicsStatus.FAIL,
                data_eligible=False,
                controls={
                    "basin_mechanism_retained": True,
                    "exactness_canary": False,
                },
                metrics={"exactness_cases_executed": float(canary.cases_executed)},
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reason_codes=[
                    "EXACTNESS_CANARY_FAIL",
                    f"CASE_{canary.failed_case_id}",
                ],
            )
        decision = super().l0(ticket, item)
        return decision.model_copy(
            update={
                "controls": {
                    **decision.controls,
                    "basin_mechanism_retained": True,
                    "exactness_canary": True,
                },
                "metrics": {
                    **decision.metrics,
                    "exactness_cases_executed": float(canary.cases_executed),
                },
            }
        )

    def l1(self, ticket: CandidateTicket, item: Any, l0: FunnelDecision) -> FunnelDecision:
        if not l0.continue_pipeline:
            raise ValueError("L1 cannot run after an L0 block")
        raw = self._run(item, seeds=self.policy.probe_seeds, repeats=self.policy.probe_repeats)
        valid = bool(raw.get("correct")) and not bool(raw.get("adapter_exception"))
        speedup = float(raw.get("raw_speedup", 0.0))
        threshold = self.local_incumbents[ticket.lineage_id] * self.probe_parent_fraction
        promoted = valid and speedup >= threshold
        reasons = ["BASIN_LOCAL_PROBE_PROMOTE" if promoted else "BASIN_LOCAL_PROBE_BLOCK"]
        if failure_reason := self._failure_reason(raw):
            reasons.append(failure_reason)
        return FunnelDecision(
            stage=FunnelStage.L1,
            status=StageStatus.PASS if promoted else StageStatus.BLOCK,
            continue_pipeline=promoted,
            mechanics_status=MechanicsStatus.PASS if valid else MechanicsStatus.FAIL,
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
            reason_codes=reasons,
        )


class M2R3CAutonomousCampaignRunner(M2AutonomousCampaignRunner):
    """R3C runner with decoded briefs and a machine-validated refinement plan."""

    def __init__(
        self,
        *args: Any,
        r3c_policy: M2R3CPolicy,
        context_run_dirs: list[Path],
        **kwargs: Any,
    ) -> None:
        self.r3c_policy = r3c_policy
        self._basins = {item.basin_id: item for item in r3c_policy.basins}
        self._candidate_contracts: dict[str, dict[str, str]] = {}
        self._plans: dict[str, BasinRefinementPlan] = {}
        self._r3c_lock = threading.Lock()
        self._context_run_dirs = tuple(path.resolve() for path in context_run_dirs)
        self._validate_context_runs()
        super().__init__(*args, **kwargs)
        if self.policy != r3c_policy.frozen_base_policy():
            raise ValueError("runner base policy does not match M2-R3C policy")
        self._bind_context()

    def _validate_context_runs(self) -> None:
        prohibited = {EvidencePermission.CONFIRM, EvidencePermission.CLAIM}
        for run_dir in self._context_run_dirs:
            contract = load_contract(run_dir / "contract.locked.yaml")
            permissions = {
                permission
                for source in contract.evidence_sources
                for permission in source.permissions
            }
            if (
                contract.lock is None
                or contract.budgets.confirmation_runs != 0
                or contract.authority.confirmation_visible_to_agents
                or permissions & prohibited
                or "DEVELOPMENT_ONLY" not in contract.campaign.claim_scope
            ):
                raise ValueError("M2-R3C context must be locked and development-only")

    def _bind_context(self) -> None:
        payload = {
            "schema_version": "1.0",
            "policy_sha256": sha256_object(self.r3c_policy.model_dump(mode="json")),
            "context_runs": [
                {
                    "path": str(path),
                    "contract_sha256": load_contract(path / "contract.locked.yaml").lock.content_sha256,
                    "manifest_sha256": sha256_file(path / "run_manifest.json"),
                }
                for path in self._context_run_dirs
            ],
            "feedback_transport": "DIRECT_DECODED_BOUNDED_BRIEF",
            "scientific_authority": "NONE_SCHEDULING_ONLY",
            "blind_artifacts_read": False,
            "confirmation_runs": 0,
        }
        path = self.run_dir / "m2_r3c_context_manifest.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != payload:
                raise ValueError("M2-R3C context manifest drift")
        else:
            create_once_json(path, payload)

    @staticmethod
    def _generation_id(candidate_id: str) -> str:
        return candidate_id.rsplit("-C", 1)[0]

    def _brief(self, candidate_id: str) -> BasinRefinementBrief:
        generation_id = self._generation_id(candidate_id)
        path = (
            self.run_dir
            / "generations"
            / generation_id
            / "r3c_feedback"
            / f"{candidate_id}.brief.json"
        )
        return BasinRefinementBrief.model_validate_json(path.read_text(encoding="utf-8"))

    def _refinement_plan(self, candidate_id: str) -> BasinRefinementPlan:
        generation_id = self._generation_id(candidate_id)
        directory = self.run_dir / "generations" / generation_id / "r3c_feedback"
        plan_path = directory / f"{candidate_id}.plan.json"
        brief = self._brief(candidate_id)
        if plan_path.exists():
            return BasinRefinementPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        self.budgets.reserve(
            "proposal_calls", 1, f"r3c_refinement_plan:{candidate_id}"
        )
        schema = _codex_output_schema(BasinRefinementPlan.model_json_schema())
        properties = schema["properties"]
        for field, value in (
            ("candidate_id", candidate_id),
            ("basin_id", brief.basin_id),
            ("parent_candidate_id", brief.parent_candidate_id),
            ("brief_id", brief.brief_id),
        ):
            properties[field] = {"const": value, "type": "string"}
        counters = sorted(brief.available_counters)
        properties["observed_counter"] = {"enum": counters, "type": "string"}
        properties["target_counter"] = {"enum": counters, "type": "string"}
        allowed_seeds = sorted(set(brief.regression_seed_ids + brief.improvement_seed_ids))
        properties["addressed_seed_ids"]["items"] = {
            "enum": allowed_seeds,
            "type": "integer",
        }
        schema_path = directory / f"{candidate_id}.plan.schema.json"
        atomic_write_json(schema_path, schema)
        raw_path = directory / f"{candidate_id}.plan.raw.json"
        result = self.backend.run(
            role=CodexRole("hypothesis_explorer"),
            prompt=(
                "Return one M2-R3C BasinRefinementPlan matching the schema. Select a "
                "measured counter and regression seed cluster from the decoded brief. "
                "Do not infer an unmeasured hotspot. Preserve exactness and the frozen "
                "basin mechanism. Feedback is development-only scheduling context.\n"
                + brief.model_dump_json(indent=2)
            ),
            workdir=self._proposal_workspace(),
            output_schema=schema_path,
            output_path=raw_path,
            events_path=directory / f"{candidate_id}.plan.events.jsonl",
            stderr_path=directory / f"{candidate_id}.plan.stderr.log",
            timeout_seconds=self.timeout_seconds,
        )
        if result.get("status") != "PASS" or not raw_path.is_file():
            raise RuntimeError(f"M2-R3C planning failed: {candidate_id}")
        plan = BasinRefinementPlan.model_validate_json(raw_path.read_text(encoding="utf-8"))
        expected = brief.available_counters[plan.observed_counter]
        if abs(plan.observed_value - expected) > max(1e-9, abs(expected) * 1e-6):
            raise ValueError("M2-R3C plan misquoted its observed counter")
        allowed = set(brief.regression_seed_ids + brief.improvement_seed_ids)
        if not set(plan.addressed_seed_ids).issubset(allowed):
            raise ValueError("M2-R3C plan cited seeds outside the frozen brief")
        create_once_json(plan_path, plan)
        raw_path.unlink(missing_ok=True)
        return plan

    def _propose_candidate(self, **kwargs: Any) -> CampaignCandidate:
        feedback = kwargs["feedback"]
        raw_operator = feedback.get("async_operator_contract")
        if not isinstance(raw_operator, dict):
            raise ValueError("M2-R3C proposal lacks async operator contract")
        raw = json.loads(str(raw_operator.get("operator_directive", "")))
        contract = {
            key: str(raw.get(key, ""))
            for key in ("basin_id", "parent_candidate_id", "brief_sha256")
        }
        candidate_id = f"{kwargs['generation_id']}-C{kwargs['slot']:02d}"
        brief = self._brief(candidate_id)
        brief_path = (
            self.run_dir
            / "generations"
            / kwargs["generation_id"]
            / "r3c_feedback"
            / f"{candidate_id}.brief.json"
        )
        if contract["basin_id"] not in self._basins:
            raise ValueError("M2-R3C contract references unknown basin")
        if contract["parent_candidate_id"] != brief.parent_candidate_id:
            raise ValueError("M2-R3C brief parent drift")
        if contract["brief_sha256"] != sha256_file(brief_path):
            raise ValueError("M2-R3C brief hash drift")
        if kwargs["mode"] is not DiscoveryMode.NORMAL or kwargs[
            "required_mutation"
        ] is not MutationType.MECHANISM:
            raise ValueError("M2-R3C permits only normal BASIN_REFINE")
        with self._r3c_lock:
            self._candidate_contracts[candidate_id] = contract
            self._plans[candidate_id] = self._refinement_plan(candidate_id)
        return super()._propose_candidate(**kwargs)

    def _proposal_prompt(self, **kwargs: Any) -> str:
        prompt = super()._proposal_prompt(**kwargs)
        candidate_id = str(kwargs["candidate_id"])
        brief = self._brief(candidate_id)
        plan = self._plans[candidate_id]
        basin = self._basins[brief.basin_id]
        return (
            prompt
            + "\nM2-R3C uses the decoded bounded brief and validated plan below. The "
            "CandidateGenome must implement that measured intervention, name the observed "
            "and target counters in its mechanism claims, preserve all correctness "
            "invariants, use only the sole parent, and keep the exact family. Do not "
            "replace the measured plan with a code-reading guess.\n"
            f"Exact family: {basin.family}\n"
            f"Required source anchors: {json.dumps(basin.source_markers)}\n"
            "Decoded refinement brief:\n"
            + brief.model_dump_json(indent=2)
            + "\nValidated refinement plan:\n"
            + plan.model_dump_json(indent=2)
        )

    def _validate_proposal_vocabulary(self, candidate: CampaignCandidate) -> None:
        super()._validate_proposal_vocabulary(candidate)
        genome = candidate.acquisition.candidate
        contract = self._candidate_contracts.get(genome.candidate_id)
        plan = self._plans.get(genome.candidate_id)
        if contract is None or plan is None:
            raise ValueError("M2-R3C candidate lacks frozen plan context")
        basin = self._basins[contract["basin_id"]]
        if genome.genetic_parent_id != plan.parent_candidate_id or genome.parent_ids != [
            plan.parent_candidate_id
        ]:
            raise ValueError("M2-R3C forbids crossover and non-local parents")
        if genome.family != basin.family or genome.mutation_type is not MutationType.MECHANISM:
            raise ValueError("M2-R3C candidate escaped its frozen basin")
        text = " ".join(
            [genome.hypothesis, genome.intervention, *genome.mechanism_claims]
        ).casefold()
        if plan.observed_counter.casefold() not in text or plan.target_counter.casefold() not in text:
            raise ValueError("M2-R3C proposal omitted measured plan counters")
        plan_terms = {
            token
            for token in plan.mechanism_change.casefold().replace("_", " ").split()
            if len(token) >= 5
        }
        if len(plan_terms & set(text.replace("_", " ").split())) < min(3, len(plan_terms)):
            raise ValueError("M2-R3C proposal did not implement the validated plan")


__all__ = [
    "ActivationInstance",
    "AttemptFeedback",
    "BasinActivationProfile",
    "BasinRefinementBrief",
    "BasinRefinementCStagedAdapter",
    "BasinRefinementPlan",
    "ExactnessCanaryResult",
    "M2R3CAutonomousCampaignRunner",
    "M2R3CPolicy",
    "StageFeedback",
    "activation_profile_commit",
    "run_exactness_canary",
]
