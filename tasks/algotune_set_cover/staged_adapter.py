"""Development-only L0/L1/L2 funnel for asynchronous Set Cover search."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from evidence_evolve.discovery.campaign import CampaignCandidate, EvaluationRun
from evidence_evolve.discovery.throughput import (
    CandidateTicket,
    FunnelDecision,
    FunnelStage,
    StageStatus,
)
from evidence_evolve.models import (
    MechanicsStatus,
    ResearchContract,
    ScientificOutcome,
    StrictModel,
)
from tasks.algotune_set_cover.campaign_evaluator import build_evaluation
from tasks.algotune_set_cover.common import DEVELOPMENT_SEEDS, evaluate_candidate


class MaterializedSetCoverCandidate(Protocol):
    item: CampaignCandidate
    worktree: Path
    changed_files: list[str]
    genetic_parent_id: str
    genetic_parent_commit: str
    candidate_commit: str
    candidate_ref: str
    patch_sha256: str
    parent_patch_sha256: str


RawEvaluator = Callable[[str | Path, Iterable[int], int], dict[str, Any]]
StructuralCheck = Callable[[CandidateTicket, MaterializedSetCoverCandidate], bool]


class _StructureNormalizer(ast.NodeTransformer):
    """Remove cosmetic identifiers and literal tuning values from an AST shape."""

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Constant(value=None), node)

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.arg(arg="_", annotation=None), node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
        normalized = self.generic_visit(node)
        normalized.name = "_"
        return normalized


def _normalized_ast_shape(source: str) -> str:
    tree = _StructureNormalizer().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


class SetCoverStructuralTransitionAudit:
    """Conservative scheduling-only check for a non-cosmetic code transition."""

    def __init__(
        self,
        *,
        repo_root: Path,
        parent_families: dict[str, str],
        operator_plan_dir: Path | None = None,
        candidate_relative_path: str = "tasks/algotune_set_cover/initial.py",
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.parent_families = dict(parent_families)
        self.operator_plan_dir = (
            operator_plan_dir.resolve() if operator_plan_dir is not None else None
        )
        self.candidate_relative_path = candidate_relative_path

    def __call__(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
    ) -> bool:
        if not ticket.requires_structural_transition:
            return False
        candidate = item.item.acquisition.candidate
        parent_family = self.parent_families.get(item.genetic_parent_id)
        if parent_family is None or (
            candidate.family.strip().casefold() == parent_family.strip().casefold()
        ):
            return False
        completed = subprocess.run(
            [
                "git",
                "show",
                f"{item.genetic_parent_commit}:{self.candidate_relative_path}",
            ],
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return False
        try:
            parent_shape = _normalized_ast_shape(completed.stdout)
            candidate_shape = _normalized_ast_shape(
                self.candidate_path(item).read_text(encoding="utf-8")
            )
        except (OSError, SyntaxError):
            return False
        return candidate_shape != parent_shape

    @staticmethod
    def _mechanism_family(text: str) -> str | None:
        normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold())
        token_set = set(normalized.split())
        rules = (
            ("sat_maxsat", lambda: "sat" in token_set or "maxsat" in token_set),
            (
                "primal_dual",
                lambda: "primal" in token_set and "dual" in token_set,
            ),
            (
                "constraint_incidence_branching",
                lambda: "incidence" in token_set
                and bool({"branch", "branching", "pivot"} & token_set),
            ),
            (
                "meet_in_the_middle",
                lambda: "meet" in token_set
                and "middle" in token_set,
            ),
            (
                "local_search",
                lambda: "local" in token_set and "search" in token_set,
            ),
            (
                "mathematical_relaxation",
                lambda: bool({"relaxation", "lagrangian", "linear"} & token_set),
            ),
            (
                "dynamic_programming",
                lambda: "dynamic" in token_set and "programming" in token_set,
            ),
            (
                "deterministic_greedy",
                lambda: "greedy" in token_set
                or ({"marginal", "constructive"} <= token_set),
            ),
        )
        return next((name for name, matches in rules if matches()), None)

    def root_key(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
    ) -> str | None:
        if not self(ticket, item):
            return None
        candidate_id = item.item.acquisition.candidate.candidate_id
        if self.operator_plan_dir is not None:
            path = self.operator_plan_dir / f"{candidate_id}.escape_plan.json"
            if path.is_file():
                try:
                    plan = json.loads(path.read_text(encoding="utf-8"))
                    mechanism = str(plan.get("replacement_mechanism", ""))
                    if family := self._mechanism_family(mechanism):
                        return family
                except (OSError, json.JSONDecodeError):
                    return None
        try:
            shape = _normalized_ast_shape(
                self.candidate_path(item).read_text(encoding="utf-8")
            )
        except (OSError, SyntaxError):
            return None
        return "static_ast_" + hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]

    def candidate_path(self, item: MaterializedSetCoverCandidate) -> Path:
        return item.worktree / self.candidate_relative_path


def _default_evaluator(
    candidate_path: str | Path,
    seeds: Iterable[int],
    repeats: int,
) -> dict[str, Any]:
    return evaluate_candidate(candidate_path, seeds, repeats=repeats)


class SetCoverFunnelPolicy(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_relative_path: str = "tasks/algotune_set_cover/initial.py"
    mechanics_seeds: list[int] = Field(
        default_factory=lambda: [10_000, 10_001], min_length=1
    )
    probe_seeds: list[int] = Field(
        default_factory=lambda: list(DEVELOPMENT_SEEDS[:8]), min_length=1
    )
    full_development_seeds: list[int] = Field(
        default_factory=lambda: list(DEVELOPMENT_SEEDS), min_length=1
    )
    mechanics_repeats: int = Field(default=1, ge=1)
    probe_repeats: int = Field(default=1, ge=1)
    full_development_repeats: int = Field(default=3, ge=1)
    probe_min_speedup: float = Field(default=1.0, ge=0.0)
    incumbent_speedup: float = Field(ge=0.0)
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    blind_artifacts_permitted: Literal[False] = False
    confirmation_permitted: Literal[False] = False

    @model_validator(mode="after")
    def sampling_sets_are_frozen_and_disjoint(self) -> "SetCoverFunnelPolicy":
        if self.evidence_scope != "DEVELOPMENT_ONLY":
            raise ValueError("Set Cover funnel must remain development-only")
        if self.blind_artifacts_permitted or self.confirmation_permitted:
            raise ValueError("Set Cover development funnel cannot read blind/confirmation")
        if len(set(self.mechanics_seeds)) != len(self.mechanics_seeds):
            raise ValueError("mechanics seeds must be unique")
        if len(set(self.probe_seeds)) != len(self.probe_seeds):
            raise ValueError("probe seeds must be unique")
        if len(set(self.full_development_seeds)) != len(
            self.full_development_seeds
        ):
            raise ValueError("full development seeds must be unique")
        if set(self.mechanics_seeds) & set(self.full_development_seeds):
            raise ValueError("mechanics canary seeds cannot overlap development")
        if not set(self.probe_seeds).issubset(self.full_development_seeds):
            raise ValueError("probe seeds must be a fixed subset of full development")
        return self


class SetCoverStagedAdapter:
    """Run fixed sampling stages without granting L0/L1 scientific authority."""

    def __init__(
        self,
        *,
        contract: ResearchContract,
        policy: SetCoverFunnelPolicy,
        evaluator: RawEvaluator | None = None,
        structural_check: StructuralCheck | None = None,
    ) -> None:
        if contract.lock is None:
            raise ValueError("staged adapter requires a locked contract")
        self.contract = contract
        self.policy = policy
        self.evaluator = evaluator or _default_evaluator
        self.structural_check = structural_check or (lambda _ticket, _item: False)

    def candidate_path(self, item: MaterializedSetCoverCandidate) -> Path:
        path = (item.worktree / self.policy.candidate_relative_path).resolve()
        path.relative_to(item.worktree.resolve())
        if not path.is_file():
            raise FileNotFoundError(f"materialized Set Cover candidate missing: {path}")
        return path

    def _run(
        self,
        item: MaterializedSetCoverCandidate,
        *,
        seeds: list[int],
        repeats: int,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            raw = dict(self.evaluator(self.candidate_path(item), seeds, repeats))
            raw.setdefault("elapsed_seconds", time.perf_counter() - started)
            raw["adapter_exception"] = False
            return raw
        except Exception as exc:
            return {
                "raw_speedup": 0.0,
                "valid_rate": 0.0,
                "correct": False,
                "instance_count": len(seeds),
                "elapsed_seconds": time.perf_counter() - started,
                "failure": f"{type(exc).__name__}:{exc}",
                "adapter_exception": True,
            }

    @staticmethod
    def _controls(raw: dict[str, Any]) -> dict[str, bool]:
        return {
            "candidate_valid": bool(raw.get("correct")),
            "development_only": True,
        }

    @staticmethod
    def _outcome(raw: dict[str, Any]) -> ScientificOutcome:
        if bool(raw.get("adapter_exception")) or not bool(raw.get("correct")):
            return ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
        if bool(raw.get("correct")) and float(raw.get("raw_speedup", 0.0)) > 1.0:
            return ScientificOutcome.POSITIVE_HEADROOM
        return ScientificOutcome.VALID_NEGATIVE

    @staticmethod
    def _failure_reason(raw: dict[str, Any]) -> str | None:
        failure = str(raw.get("failure", "")).strip()
        if not failure:
            return None
        category = failure.split(":", 1)[0].strip().upper()
        safe = "".join(character if character.isalnum() else "_" for character in category)
        return f"EVALUATOR_{safe or 'FAILURE'}"

    def l0(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
    ) -> FunnelDecision:
        del ticket
        raw = self._run(
            item,
            seeds=self.policy.mechanics_seeds,
            repeats=self.policy.mechanics_repeats,
        )
        passed = bool(raw.get("correct")) and not bool(raw.get("adapter_exception"))
        reason_codes = [
            "SYNTHETIC_MECHANICS_PASS" if passed else "SYNTHETIC_MECHANICS_FAIL"
        ]
        if failure_reason := self._failure_reason(raw):
            reason_codes.append(failure_reason)
        return FunnelDecision(
            stage=FunnelStage.L0,
            status=StageStatus.PASS if passed else StageStatus.BLOCK,
            continue_pipeline=passed,
            mechanics_status=(
                MechanicsStatus.PASS if passed else MechanicsStatus.FAIL
            ),
            data_eligible=False,
            controls={
                "candidate_valid": bool(raw.get("correct")),
                "synthetic_only": True,
            },
            metrics={
                "mechanics_valid_rate": float(raw.get("valid_rate", 0.0)),
                "mechanics_elapsed_seconds": float(raw["elapsed_seconds"]),
                "evaluator_worker_count": float(raw.get("worker_count", 1)),
            },
            scientific_outcome=(
                ScientificOutcome.NOT_EVALUABLE_DATA
                if passed
                else ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
            ),
            reason_codes=reason_codes,
        )

    def l1(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
        l0: FunnelDecision,
    ) -> FunnelDecision:
        del ticket
        if not l0.continue_pipeline:
            raise ValueError("L1 cannot run after an L0 block")
        raw = self._run(
            item,
            seeds=self.policy.probe_seeds,
            repeats=self.policy.probe_repeats,
        )
        valid = bool(raw.get("correct")) and not bool(raw.get("adapter_exception"))
        speedup = float(raw.get("raw_speedup", 0.0))
        promoted = valid and speedup >= self.policy.probe_min_speedup
        reason_codes = [
            "FROZEN_PROBE_PROMOTE" if promoted else "FROZEN_PROBE_BLOCK"
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
                "evaluator_worker_count": float(raw.get("worker_count", 1)),
            },
            scientific_outcome=self._outcome(raw),
            reason_codes=reason_codes,
        )

    def full_evaluation(
        self,
        item: MaterializedSetCoverCandidate,
    ) -> EvaluationRun:
        raw = self._run(
            item,
            seeds=self.policy.full_development_seeds,
            repeats=self.policy.full_development_repeats,
        )
        wrapped = {
            "mechanics_status": (
                "FAIL" if raw.get("adapter_exception") else "PASS"
            ),
            "metrics": {
                "invalid_solution_rate": 1.0 - float(raw.get("valid_rate", 0.0)),
                "raw_speedup": float(raw.get("raw_speedup", 0.0)),
            },
            "controls": self._controls(raw),
            "error": str(raw.get("failure", "")),
        }
        evaluation = build_evaluation(
            contract_sha256=self.contract.lock.content_sha256,
            candidate=item.item,
            changed_files=item.changed_files,
            raw=wrapped,
        )
        return EvaluationRun(
            evaluation=evaluation,
            command=[
                "in-process",
                "algotune-set-cover-full-development-funnel",
                f"instances={len(self.policy.full_development_seeds)}",
                f"repeats={self.policy.full_development_repeats}",
                f"workers={int(raw.get('worker_count', 1))}",
            ],
            elapsed_seconds=float(raw["elapsed_seconds"]),
            seed=0,
            genetic_parent_id=item.genetic_parent_id,
            genetic_parent_commit=item.genetic_parent_commit,
            candidate_commit=item.candidate_commit,
            candidate_ref=item.candidate_ref,
            patch_sha256=item.patch_sha256,
            parent_patch_sha256=item.parent_patch_sha256,
        )

    def promotion_worthy(self, evaluation: EvaluationRun) -> bool:
        controls = evaluation.evaluation.controls
        return bool(
            controls
            and all(controls.values())
            and evaluation.evaluation.metrics.get("raw_speedup", 0.0)
            > self.policy.incumbent_speedup
        )

    def structural_transition_pass(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
    ) -> bool:
        return bool(
            ticket.requires_structural_transition
            and self.structural_check(ticket, item)
        )

    def structural_root_key(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
    ) -> str | None:
        if not ticket.requires_structural_transition:
            return None
        root_key = getattr(self.structural_check, "root_key", None)
        if callable(root_key):
            return root_key(ticket, item)
        if not self.structural_check(ticket, item):
            return None
        family = item.item.acquisition.candidate.family.strip().casefold()
        safe = re.sub(r"[^a-z0-9]+", "_", family).strip("_")
        return f"claimed_family_{safe}"


class SetCoverProfiledStagedAdapter(SetCoverStagedAdapter):
    """R4 funnel that retains external latency tails and diagnostic cost counters."""

    PROFILE_METRICS = (
        "wall_time_ns",
        "wall_time_p50_ns",
        "wall_time_p95_ns",
        "wall_time_p99_ns",
        "node_expansions",
        "bound_time_ns",
        "cache_time_ns",
        "reduction_ratio",
    )

    @classmethod
    def _profile_metrics(cls, raw: dict[str, Any]) -> dict[str, float]:
        return {name: float(raw.get(name, 0.0)) for name in cls.PROFILE_METRICS}

    def l0(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
    ) -> FunnelDecision:
        raw = self._run(
            item,
            seeds=self.policy.mechanics_seeds,
            repeats=self.policy.mechanics_repeats,
        )
        exact = bool(raw.get("correct")) and not bool(raw.get("adapter_exception"))
        profile_complete = bool(raw.get("telemetry_available"))
        structural = bool(self.structural_check(ticket, item))
        passed = exact and profile_complete and structural
        reason_codes = [
            "R4_L0_ADMISSION_PASS" if passed else "R4_L0_ADMISSION_BLOCK"
        ]
        if not exact:
            reason_codes.append("SYNTHETIC_MECHANICS_FAIL")
        if not profile_complete:
            reason_codes.append("PROFILE_CONTRACT_INCOMPLETE")
        if not structural:
            reason_codes.append("CLOSED_BASIN_NOT_EXITED")
        if failure_reason := self._failure_reason(raw):
            reason_codes.append(failure_reason)
        root_key = self.structural_root_key(ticket, item) if structural else None
        return FunnelDecision(
            stage=FunnelStage.L0,
            status=StageStatus.PASS if passed else StageStatus.BLOCK,
            continue_pipeline=passed,
            mechanics_status=(MechanicsStatus.PASS if exact else MechanicsStatus.FAIL),
            data_eligible=False,
            controls={
                "candidate_valid": exact,
                "synthetic_only": True,
                "profile_contract_complete": profile_complete,
                "closed_basin_exited": structural,
            },
            metrics={
                "mechanics_valid_rate": float(raw.get("valid_rate", 0.0)),
                "mechanics_elapsed_seconds": float(raw["elapsed_seconds"]),
                "evaluator_worker_count": float(raw.get("worker_count", 1)),
                **self._profile_metrics(raw),
            },
            scientific_outcome=(
                ScientificOutcome.NOT_EVALUABLE_DATA
                if exact
                else ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
            ),
            reason_codes=reason_codes,
            structural_transition_pass=structural,
            structural_root_key=root_key,
        )

    def l1(
        self,
        ticket: CandidateTicket,
        item: MaterializedSetCoverCandidate,
        l0: FunnelDecision,
    ) -> FunnelDecision:
        del ticket
        if not l0.continue_pipeline:
            raise ValueError("L1 cannot run after an L0 block")
        raw = self._run(
            item,
            seeds=self.policy.probe_seeds,
            repeats=self.policy.probe_repeats,
        )
        valid = bool(raw.get("correct")) and not bool(raw.get("adapter_exception"))
        speedup = float(raw.get("raw_speedup", 0.0))
        promoted = valid and speedup >= self.policy.probe_min_speedup
        reason_codes = [
            "R4_WALL_CLOCK_PROBE_PROMOTE" if promoted else "R4_WALL_CLOCK_PROBE_BLOCK"
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
                "evaluator_worker_count": float(raw.get("worker_count", 1)),
                **self._profile_metrics(raw),
            },
            scientific_outcome=self._outcome(raw),
            reason_codes=reason_codes,
        )

    def full_evaluation(
        self,
        item: MaterializedSetCoverCandidate,
    ) -> EvaluationRun:
        raw = self._run(
            item,
            seeds=self.policy.full_development_seeds,
            repeats=self.policy.full_development_repeats,
        )
        wrapped = {
            "mechanics_status": "FAIL" if raw.get("adapter_exception") else "PASS",
            "metrics": {
                "invalid_solution_rate": 1.0 - float(raw.get("valid_rate", 0.0)),
                "raw_speedup": float(raw.get("raw_speedup", 0.0)),
                **self._profile_metrics(raw),
            },
            "controls": self._controls(raw),
            "error": str(raw.get("failure", "")),
        }
        evaluation = build_evaluation(
            contract_sha256=self.contract.lock.content_sha256,
            candidate=item.item,
            changed_files=item.changed_files,
            raw=wrapped,
        )
        return EvaluationRun(
            evaluation=evaluation,
            command=[
                "in-process",
                "algotune-set-cover-r4-profiled-development-funnel",
                f"instances={len(self.policy.full_development_seeds)}",
                f"repeats={self.policy.full_development_repeats}",
            ],
            elapsed_seconds=float(raw["elapsed_seconds"]),
            seed=0,
            genetic_parent_id=item.genetic_parent_id,
            genetic_parent_commit=item.genetic_parent_commit,
            candidate_commit=item.candidate_commit,
            candidate_ref=item.candidate_ref,
            patch_sha256=item.patch_sha256,
            parent_patch_sha256=item.parent_patch_sha256,
        )


__all__ = [
    "MaterializedSetCoverCandidate",
    "SetCoverFunnelPolicy",
    "SetCoverStructuralTransitionAudit",
    "SetCoverStagedAdapter",
    "SetCoverProfiledStagedAdapter",
]
