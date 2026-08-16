"""M2-R4 structural-basin jump with generation-time mechanism diversity gates."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.backends.codex_cli import CodexRole
from evidence_evolve.discovery.autonomous import _codex_output_schema
from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.discovery.m2_escape import M2AutonomousCampaignRunner
from evidence_evolve.discovery.m2_r2_escape import (
    M2R2AutonomousCampaignRunner,
    M2R2Policy,
    StructuralEscapePlan,
)
from evidence_evolve.meta_evolution.policy import DiscoveryMode
from evidence_evolve.models import MutationType, StrictModel


class StructuralBasinSpec(StrictModel):
    basin_id: str = Field(min_length=2)
    mechanism_signature: list[str] = Field(min_length=2)
    directive: str = Field(min_length=12)

    @model_validator(mode="after")
    def canonical_signature(self) -> "StructuralBasinSpec":
        if len(self.mechanism_signature) != len(set(self.mechanism_signature)):
            raise ValueError("mechanism signatures must be unique")
        return self


class M2R4Policy(M2R2Policy):
    """Frozen development-only policy for one eight-root structural wave."""

    closed_basin_id: Literal["pivot_branch_and_bound"]
    closure_interpretation: Literal["PIVOT_BNB_LOCAL_REFINEMENT_NOT_SUPPORTED"]
    closure_source: str
    conversion_threshold: float = Field(gt=0.0)
    probe_min_speedup: float = Field(gt=0.0)
    candidate_slots: Literal[8] = 8
    maximum_signature_jaccard: float = Field(default=0.6, ge=0.0, lt=1.0)
    required_profile_metrics: list[str] = Field(min_length=8)
    prohibited_source_markers: list[str] = Field(min_length=1)
    prohibited_claim_terms: list[str] = Field(min_length=1)
    basins: list[StructuralBasinSpec] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def structural_roots_are_distinct_and_conversion_is_frozen(self) -> "M2R4Policy":
        if self.probe_min_speedup >= self.conversion_threshold:
            raise ValueError("R4 probe threshold must remain below conversion threshold")
        if self.escape_budget_generations != self.candidate_slots:
            raise ValueError("R4 escape budget must equal the structural-root count")
        ids = [basin.basin_id for basin in self.basins]
        if len(ids) != len(set(ids)):
            raise ValueError("R4 basin ids must be unique")
        prohibited = {item.casefold() for item in self.prohibited_claim_terms}
        for basin in self.basins:
            if prohibited & {item.casefold() for item in basin.mechanism_signature}:
                raise ValueError("R4 signature contains a prohibited pivot mechanism")
        for index, left in enumerate(self.basins):
            left_terms = set(left.mechanism_signature)
            for right in self.basins[index + 1 :]:
                right_terms = set(right.mechanism_signature)
                similarity = len(left_terms & right_terms) / len(left_terms | right_terms)
                if similarity > self.maximum_signature_jaccard:
                    raise ValueError(
                        f"R4 signatures overlap too strongly: {left.basin_id}, "
                        f"{right.basin_id} ({similarity:.3f})"
                    )
        return self

    def basin(self, basin_id: str) -> StructuralBasinSpec:
        return next(item for item in self.basins if item.basin_id == basin_id)


class R4StructuralEscapePlan(StructuralEscapePlan):
    basin_id: str
    mechanism_signature: list[str] = Field(min_length=2)
    inherited_pivot_bnb_removed: Literal[True] = True
    wall_clock_cost_hypothesis: str = Field(min_length=12)
    profiling_contract: list[str] = Field(min_length=8)


def _generation_id(candidate_id: str) -> str:
    marker = candidate_id.rfind("-C")
    if marker <= 0:
        raise ValueError(f"candidate id has no generation prefix: {candidate_id}")
    return candidate_id[:marker]


class M2R4AutonomousCampaignRunner(M2R2AutonomousCampaignRunner):
    """R2 context compiler plus a frozen, machine-checked R4 basin assignment."""

    def __init__(self, *args: Any, r4_policy: M2R4Policy, **kwargs: Any) -> None:
        super().__init__(*args, r2_policy=r4_policy, **kwargs)
        self.r4_policy = r4_policy

    def _escape_plan(
        self,
        *,
        candidate_id: str,
        generation_id: str,
        eligible_parents: list[str],
        operator_contract: dict[str, str] | None = None,
    ) -> R4StructuralEscapePlan:
        if operator_contract is None:
            raise ValueError("R4 requires a frozen async operator contract")
        basin_id = operator_contract.get("operator_class", "")
        basin = self.r4_policy.basin(basin_id)
        if operator_contract.get("operator_directive") != basin.directive:
            raise ValueError("R4 operator directive drift")
        operator_dir = self.run_dir / "generations" / generation_id / "r4_operator"
        plan_path = operator_dir / f"{candidate_id}.escape_plan.json"
        if plan_path.is_file():
            plan = R4StructuralEscapePlan.model_validate_json(
                plan_path.read_text(encoding="utf-8")
            )
            self._validate_r4_plan(
                plan,
                candidate_id,
                basin,
                eligible_parents,
                self.r4_policy.required_profile_metrics,
            )
            return plan

        parents = [parent for parent in eligible_parents if parent != "SEED"]
        if not parents:
            raise ValueError("R4 structural root requires a non-SEED interface parent")
        failure_model = self._failure_model(
            candidate_id=candidate_id,
            generation_id=generation_id,
            eligible_parents=parents,
        )
        self.budgets.reserve(
            "proposal_calls", 1, f"r4_operator_plan:{generation_id}:{candidate_id}"
        )
        schema = _codex_output_schema(R4StructuralEscapePlan.model_json_schema())
        properties = schema["properties"]
        constants: dict[str, Any] = {
            "candidate_id": candidate_id,
            "operator_class": basin_id,
            "operator_directive": basin.directive,
            "basin_id": basin_id,
            "mechanism_signature": basin.mechanism_signature,
            "inherited_pivot_bnb_removed": True,
            "profiling_contract": self.r4_policy.required_profile_metrics,
        }
        for name, value in constants.items():
            if isinstance(value, list):
                properties[name] = {
                    "type": "array",
                    "items": {"type": "string", "enum": value},
                    "minItems": len(value),
                    "maxItems": len(value),
                }
            else:
                properties[name] = {
                    "const": value,
                    "type": "boolean" if isinstance(value, bool) else "string",
                }
        properties["genetic_parent_id"] = {"enum": parents, "type": "string"}
        for field in ("context_candidate_ids", "addressed_failure_candidate_ids"):
            properties[field]["items"] = {
                "enum": failure_model.source_candidate_ids,
                "type": "string",
            }
        schema_path = operator_dir / f"{candidate_id}.escape_plan.schema.json"
        atomic_write_json(schema_path, schema)
        raw_path = operator_dir / f"{candidate_id}.escape_plan.raw.json"
        prompt = (
            "You are the EvidenceEvolve M2-R4 structural-basin planner. Produce the "
            "assigned mathematical mechanism, not a pivot branch-and-bound refinement or "
            "renamed branch order. The inherited parent supplies only interface and exactness "
            "knowledge: its kernel_search/pivot_sets core must be removed. Design against "
            "measured wall-clock latency; candidate-reported counters are diagnostic only. "
            "The implementation must expose profile_snapshot() with the frozen profiling "
            "contract. All context is development-only and has no confirmation authority.\n"
            f"Assigned basin: {basin.model_dump_json(indent=2)}\n"
            f"Eligible parents: {json.dumps(parents)}\n"
            "Frozen failure context:\n"
            + failure_model.model_dump_json(indent=2)
        )
        result = self.backend.run(
            role=CodexRole("hypothesis_explorer"),
            prompt=prompt,
            workdir=self._proposal_workspace(),
            output_schema=schema_path,
            output_path=raw_path,
            events_path=operator_dir / f"{candidate_id}.escape_plan.events.jsonl",
            stderr_path=operator_dir / f"{candidate_id}.escape_plan.stderr.log",
            timeout_seconds=self.timeout_seconds,
        )
        if result.get("status") != "PASS" or not raw_path.is_file():
            raise RuntimeError(f"M2-R4 planning failed: {candidate_id}")
        plan = R4StructuralEscapePlan.model_validate_json(
            raw_path.read_text(encoding="utf-8")
        )
        self._validate_r4_plan(
            plan,
            candidate_id,
            basin,
            parents,
            self.r4_policy.required_profile_metrics,
        )
        create_once_json(plan_path, plan)
        raw_path.unlink(missing_ok=True)
        return plan

    @staticmethod
    def _validate_r4_plan(
        plan: R4StructuralEscapePlan,
        candidate_id: str,
        basin: StructuralBasinSpec,
        parents: list[str],
        profile_metrics: list[str],
    ) -> None:
        if plan.candidate_id != candidate_id or plan.basin_id != basin.basin_id:
            raise ValueError("R4 plan identity or basin drift")
        if (
            plan.operator_class != basin.basin_id
            or plan.operator_directive != basin.directive
        ):
            raise ValueError("R4 plan operator drift")
        if plan.mechanism_signature != basin.mechanism_signature:
            raise ValueError("R4 plan mechanism signature drift")
        if plan.profiling_contract != profile_metrics:
            raise ValueError("R4 plan profiling contract drift")
        if plan.genetic_parent_id not in parents:
            raise ValueError("R4 plan selected an ineligible parent")

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
        prompt = M2AutonomousCampaignRunner._proposal_prompt(
            self,
            candidate_id=candidate_id,
            generation_id=generation_id,
            island=island,
            eligible_parents=eligible_parents,
            feedback=feedback,
            required_mutation=required_mutation,
            research_action=research_action,
            mode=mode,
        )
        if mode is not DiscoveryMode.BREAKTHROUGH:
            raise ValueError("R4 permits structural breakthrough slots only")
        plan_path = (
            self.run_dir
            / "generations"
            / generation_id
            / "r4_operator"
            / f"{candidate_id}.escape_plan.json"
        )
        plan = R4StructuralEscapePlan.model_validate_json(
            plan_path.read_text(encoding="utf-8")
        )
        return (
            prompt
            + "\nM2-R4 freezes the structural plan below. The proposal must use its parent, "
            "family, signature, and falsifier. It must remove the inherited pivot-B&B core, "
            "implement profile_snapshot(), and optimize externally measured wall time rather "
            "than any internal counter. Local pivot, scheduling, bound-forwarding, cache-only, "
            "and branch-order variants are prohibited.\n"
            + plan.model_dump_json(indent=2)
        )

    def _proposal_schema(
        self,
        candidate_id: str,
        island: str,
        eligible_parents: list[str],
        required_mutation: MutationType,
    ) -> dict[str, object]:
        schema = super()._proposal_schema(
            candidate_id,
            island,
            eligible_parents,
            required_mutation,
        )
        generation_id = _generation_id(candidate_id)
        plan = R4StructuralEscapePlan.model_validate_json(
            (
                self.run_dir
                / "generations"
                / generation_id
                / "r4_operator"
                / f"{candidate_id}.escape_plan.json"
            ).read_text(encoding="utf-8")
        )
        definitions = schema["$defs"]
        definitions["CandidateGenome"]["properties"]["family"] = {
            "const": plan.basin_id,
            "type": "string",
        }
        return schema

    def _validate_proposal_vocabulary(self, candidate: CampaignCandidate) -> None:
        M2AutonomousCampaignRunner._validate_proposal_vocabulary(self, candidate)
        genome = candidate.acquisition.candidate
        generation_id = _generation_id(genome.candidate_id)
        plan = R4StructuralEscapePlan.model_validate_json(
            (
                self.run_dir
                / "generations"
                / generation_id
                / "r4_operator"
                / f"{genome.candidate_id}.escape_plan.json"
            ).read_text(encoding="utf-8")
        )
        if genome.genetic_parent_id != plan.genetic_parent_id:
            raise ValueError("R4 proposal ignored its frozen interface parent")
        if genome.family.strip().casefold() != plan.basin_id.strip().casefold():
            raise ValueError("R4 proposal ignored the frozen structural basin")
        normalized_family = re.sub(r"[^a-z0-9]+", "_", genome.family.casefold())
        if any(
            term.casefold() in normalized_family
            for term in self.r4_policy.prohibited_claim_terms
        ):
            raise ValueError("R4 proposal retained a prohibited pivot-B&B claim")


class R4SourceAudit:
    """Scheduling-only static check that the implementation crossed the closed basin."""

    def __init__(
        self,
        *,
        base_audit: Any,
        policy: M2R4Policy,
        operator_plan_dir: Path,
        candidate_relative_path: str = "tasks/algotune_set_cover/initial.py",
    ) -> None:
        self.base_audit = base_audit
        self.policy = policy
        self.operator_plan_dir = operator_plan_dir
        self.candidate_relative_path = candidate_relative_path

    def __call__(self, ticket: Any, item: Any) -> bool:
        if not self.base_audit(ticket, item):
            return False
        source = (item.worktree / self.candidate_relative_path).read_text(
            encoding="utf-8"
        )
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
        identifiers = {
            value.casefold()
            for node in ast.walk(tree)
            for value in (
                [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                else [node.id] if isinstance(node, ast.Name)
                else [node.attr] if isinstance(node, ast.Attribute)
                else []
            )
        }
        if any(
            marker.casefold() in identifiers
            for marker in self.policy.prohibited_source_markers
        ):
            return False
        if "profile_snapshot" not in identifiers:
            return False
        plan_path = self.operator_plan_dir / f"{ticket.candidate_id}.escape_plan.json"
        if not plan_path.is_file():
            return False
        plan = R4StructuralEscapePlan.model_validate_json(
            plan_path.read_text(encoding="utf-8")
        )
        return plan.basin_id == ticket.operator_class

    def root_key(self, ticket: Any, item: Any) -> str | None:
        if not self(ticket, item):
            return None
        plan = R4StructuralEscapePlan.model_validate_json(
            (self.operator_plan_dir / f"{ticket.candidate_id}.escape_plan.json").read_text(
                encoding="utf-8"
            )
        )
        return "signature_" + "__".join(plan.mechanism_signature).casefold()


__all__ = [
    "M2R4AutonomousCampaignRunner",
    "M2R4Policy",
    "R4SourceAudit",
    "R4StructuralEscapePlan",
    "StructuralBasinSpec",
]
