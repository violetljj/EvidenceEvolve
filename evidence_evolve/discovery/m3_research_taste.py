"""M3 research-taste admission primitives.

M3 evaluates planner policy rather than Set Cover performance.  Everything in
this module is development-only scheduling evidence: deterministic code ancestry,
proposal-contract fidelity, and paired-arm summaries never grant scientific or
confirmation authority.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.backends.codex_cli import CodexRole
from evidence_evolve.discovery.autonomous import _codex_output_schema
from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.discovery.m2_escape import M2AutonomousCampaignRunner
from evidence_evolve.discovery.m2_r2_escape import M2R2AutonomousCampaignRunner
from evidence_evolve.discovery.m2_r4_structural import (
    M2R4AutonomousCampaignRunner,
    R4StructuralEscapePlan,
)
from evidence_evolve.meta_evolution.policy import DiscoveryMode
from evidence_evolve.models import MutationType, ScientificOutcome, StrictModel


class PlannerArm(StrEnum):
    R4_BASELINE = "R4_BASELINE"
    MECHANISM_CONTRACT = "MECHANISM_CONTRACT"
    RESEARCH_TASTE = "RESEARCH_TASTE"


class NegativeTasteRule(StrictModel):
    rule_id: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    statement: str = Field(min_length=12)


class PlannerArmSpec(StrictModel):
    arm: PlannerArm
    proposal_budget: Literal[8] = 8
    mechanism_contract: bool
    lineage_hard_block: bool
    negative_taste_memory: bool
    contrarian_slots: list[int] = Field(default_factory=list)
    research_taste_scoring: bool

    @model_validator(mode="after")
    def arm_has_frozen_treatment(self) -> "PlannerArmSpec":
        expected = {
            PlannerArm.R4_BASELINE: (False, True, False, [], False),
            PlannerArm.MECHANISM_CONTRACT: (True, True, False, [], False),
            PlannerArm.RESEARCH_TASTE: (True, True, True, [2, 4, 6, 8], True),
        }[self.arm]
        observed = (
            self.mechanism_contract,
            self.lineage_hard_block,
            self.negative_taste_memory,
            self.contrarian_slots,
            self.research_taste_scoring,
        )
        if observed != expected:
            raise ValueError(f"M3 treatment drift for {self.arm.value}")
        return self


class M3ResearchTastePolicy(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    study_id: Literal["algotune_set_cover_m3_r0_research_taste_v0"]
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    blind_artifacts_permitted: Literal[False] = False
    confirmation_permitted: Literal[False] = False
    source_result: str
    paired_basin_ids: list[str] = Field(min_length=8, max_length=8)
    arms: list[PlannerArmSpec] = Field(min_length=3, max_length=3)
    closed_lineage_id: Literal["pivot_branch_and_bound"]
    closed_source_markers: list[str] = Field(min_length=1)
    closed_core_symbols: list[str] = Field(min_length=1)
    closed_lineage_phrases: list[str] = Field(min_length=2)
    state_representation_markers: list[str] = Field(min_length=1)
    pipeline_markers: list[str] = Field(min_length=1)
    state_pipeline_min_state_markers: Literal[1] = 1
    state_pipeline_min_pipeline_markers: Literal[2] = 2
    negative_taste_rules: list[NegativeTasteRule] = Field(min_length=3)
    primary_endpoint: Literal["genuine_structural_roots_per_8_proposals"]
    admission_target: int = Field(default=5, ge=1, le=8)
    evaluator_timeout_seconds: Literal[60] = 60
    l0_violation_reason: Literal["STRUCTURAL_ESCAPE_VIOLATION"]
    l0_violation_outcome: Literal[
        ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
    ]

    @model_validator(mode="after")
    def comparison_is_paired_and_complete(self) -> "M3ResearchTastePolicy":
        if len(set(self.paired_basin_ids)) != 8:
            raise ValueError("M3 requires eight distinct paired basin assignments")
        if [item.arm for item in self.arms] != list(PlannerArm):
            raise ValueError("M3 arms must remain baseline, hard-block, research-taste")
        rule_ids = [item.rule_id for item in self.negative_taste_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("negative-taste rule ids must be unique")
        return self


class MechanismFirstPlan(StrictModel):
    """The pre-implementation contract required in the two M3 treatment arms."""

    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    arm: Literal[PlannerArm.MECHANISM_CONTRACT, PlannerArm.RESEARCH_TASTE]
    old_mechanism: str = Field(min_length=8)
    removed_assumptions: list[str] = Field(min_length=1)
    deleted_components: list[str] = Field(min_length=1)
    new_computational_primitives: list[str] = Field(min_length=1)
    new_state_representation: str = Field(min_length=8)
    new_solver_pipeline: list[str] = Field(min_length=2)
    expected_complexity_advantage: str = Field(min_length=12)
    end_to_end_latency_thesis: str = Field(min_length=12)
    predicted_failure_mode: str = Field(min_length=8)
    information_gain_if_failed: str = Field(min_length=8)
    forbidden_dependencies: list[str] = Field(min_length=1)
    contrarian_thesis: str | None = Field(default=None, min_length=12)
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )

    @model_validator(mode="after")
    def deletion_precedes_replacement(self) -> "MechanismFirstPlan":
        old = self.old_mechanism.strip().casefold()
        primitives = {item.strip().casefold() for item in self.new_computational_primitives}
        if old in primitives:
            raise ValueError("new primitive cannot be the old mechanism renamed")
        if self.arm is PlannerArm.MECHANISM_CONTRACT and self.contrarian_thesis:
            raise ValueError("contract-only arm cannot receive contrarian treatment")
        return self


class M3StructuralEscapePlan(R4StructuralEscapePlan):
    planner_arm: Literal[PlannerArm.MECHANISM_CONTRACT, PlannerArm.RESEARCH_TASTE]
    old_mechanism: str = Field(min_length=8)
    removed_assumptions: list[str] = Field(min_length=1)
    deleted_components: list[str] = Field(min_length=1)
    new_computational_primitives: list[str] = Field(min_length=1)
    new_state_representation: str = Field(min_length=8)
    new_solver_pipeline: list[str] = Field(min_length=2)
    expected_complexity_advantage: str = Field(min_length=12)
    end_to_end_latency_thesis: str = Field(min_length=12)
    information_gain_if_failed: str = Field(min_length=8)
    forbidden_dependencies: list[str] = Field(min_length=1)
    contrarian_thesis: str | None = Field(default=None, min_length=12)

    def mechanism_first(self) -> MechanismFirstPlan:
        return MechanismFirstPlan(
            candidate_id=self.candidate_id,
            arm=self.planner_arm,
            old_mechanism=self.old_mechanism,
            removed_assumptions=self.removed_assumptions,
            deleted_components=self.deleted_components,
            new_computational_primitives=self.new_computational_primitives,
            new_state_representation=self.new_state_representation,
            new_solver_pipeline=self.new_solver_pipeline,
            expected_complexity_advantage=self.expected_complexity_advantage,
            end_to_end_latency_thesis=self.end_to_end_latency_thesis,
            predicted_failure_mode=self.predicted_failure_mode,
            information_gain_if_failed=self.information_gain_if_failed,
            forbidden_dependencies=self.forbidden_dependencies,
            contrarian_thesis=self.contrarian_thesis,
        )


class MechanismContractDecision(StrictModel):
    candidate_id: str
    passed: bool
    reason_codes: list[str]
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )


def check_mechanism_contract(
    plan: MechanismFirstPlan, policy: M3ResearchTastePolicy
) -> MechanismContractDecision:
    """Apply deterministic completeness checks; prose quality is not an authority."""

    removed = " ".join(plan.deleted_components).casefold()
    forbidden = {item.casefold() for item in plan.forbidden_dependencies}
    replacement = " ".join(
        plan.new_computational_primitives + [plan.new_state_representation]
    ).casefold()
    forbidden_text = " ".join(forbidden)
    deletion_declared = any(
        phrase.casefold() in removed for phrase in policy.closed_lineage_phrases
    )
    dependency_forbidden = any(
        phrase.casefold() in forbidden_text
        for phrase in policy.closed_lineage_phrases
    )
    retained_claims = [
        marker
        for marker in policy.closed_source_markers
        if marker.casefold() in replacement
    ]
    reasons: list[str] = []
    if not deletion_declared:
        reasons.append("CLOSED_CORE_DELETION_UNSPECIFIED")
    if not dependency_forbidden:
        reasons.append("CLOSED_CORE_DEPENDENCY_NOT_FORBIDDEN")
    if retained_claims:
        reasons.append("REPLACEMENT_RETAINS_CLOSED_MARKER")
    if not reasons:
        reasons.append("MECHANISM_FIRST_CONTRACT_PASS")
    return MechanismContractDecision(
        candidate_id=plan.candidate_id,
        passed=deletion_declared and dependency_forbidden and not retained_claims,
        reason_codes=reasons,
    )


def planner_treatment_prompt(
    policy: M3ResearchTastePolicy, arm: PlannerArm, paired_slot: int
) -> str:
    """Return the frozen policy treatment layered over identical R4 evidence."""

    if not 1 <= paired_slot <= 8:
        raise ValueError("M3 paired slot must be in [1, 8]")
    spec = next(item for item in policy.arms if item.arm is arm)
    lines = [
        f"M3 planner arm: {arm.value}.",
        f"Paired basin assignment: {policy.paired_basin_ids[paired_slot - 1]}.",
        "The development evidence packet, model, reasoning effort, token budget, "
        "implementation budget, evaluator, seeds, and arm ordering are frozen.",
    ]
    if spec.mechanism_contract:
        lines.append(
            "Before implementation, return the MechanismFirstPlan contract: old "
            "mechanism -> removed assumptions and deleted components -> new computational "
            "primitives and state -> complexity advantage -> falsifiable failure mode."
        )
    if spec.negative_taste_memory:
        lines.append("Frozen negative-taste memory:")
        lines.extend(
            f"- {rule.rule_id}: {rule.statement}"
            for rule in policy.negative_taste_rules
        )
    if paired_slot in spec.contrarian_slots:
        lines.append(
            "Contrarian slot: begin from the negation of incumbent assumptions. State "
            "which core data structure, objective decomposition, branch/bound pipeline, "
            "or exactness strategy is deleted rather than improved."
        )
    if spec.research_taste_scoring:
        lines.append(
            "Planner scheduling reward: maximize explicit assumption removal, primitive "
            "novelty, implementation fidelity, information gained if the attempt fails, "
            "and correction away from exhausted directions. Current speedup is secondary "
            "and cannot compensate for a failed hard gate."
        )
    lines.append(
        "Candidate counters and planner prose have scheduling authority only; they cannot "
        "override ancestry, exactness, telemetry, or performance gates."
    )
    return "\n".join(lines)


def _candidate_slot(candidate_id: str) -> int:
    marker = candidate_id.rfind("-C")
    if marker < 0:
        raise ValueError(f"candidate id has no slot suffix: {candidate_id}")
    return int(candidate_id[marker + 2 :])


class M3AutonomousCampaignRunner(M2R4AutonomousCampaignRunner):
    """R4-compatible runner with a single frozen M3 planner treatment."""

    def __init__(
        self,
        *args: object,
        m3_policy: M3ResearchTastePolicy,
        planner_arm: PlannerArm,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.m3_policy = m3_policy
        self.planner_arm = planner_arm

    def _m3_plan_path(self, candidate_id: str, generation_id: str) -> Path:
        return (
            self.run_dir
            / "generations"
            / generation_id
            / "m3_operator"
            / f"{candidate_id}.escape_plan.json"
        )

    def _escape_plan(
        self,
        *,
        candidate_id: str,
        generation_id: str,
        eligible_parents: list[str],
        operator_contract: dict[str, str] | None = None,
    ) -> R4StructuralEscapePlan:
        if self.planner_arm is PlannerArm.R4_BASELINE:
            return super()._escape_plan(
                candidate_id=candidate_id,
                generation_id=generation_id,
                eligible_parents=eligible_parents,
                operator_contract=operator_contract,
            )
        if operator_contract is None:
            raise ValueError("M3 requires a frozen async operator contract")
        basin_id = operator_contract.get("operator_class", "")
        basin = self.r4_policy.basin(basin_id)
        if operator_contract.get("operator_directive") != basin.directive:
            raise ValueError("M3 operator directive drift")
        plan_path = self._m3_plan_path(candidate_id, generation_id)
        if plan_path.is_file():
            return M3StructuralEscapePlan.model_validate_json(
                plan_path.read_text(encoding="utf-8")
            )
        parents = [parent for parent in eligible_parents if parent != "SEED"]
        if not parents:
            raise ValueError("M3 structural root requires a non-SEED interface parent")
        failure_model = self._failure_model(
            candidate_id=candidate_id,
            generation_id=generation_id,
            eligible_parents=parents,
        )
        self.budgets.reserve(
            "proposal_calls", 1, f"m3_operator_plan:{generation_id}:{candidate_id}"
        )
        schema = _codex_output_schema(M3StructuralEscapePlan.model_json_schema())
        properties = schema["properties"]
        constants: dict[str, object] = {
            "candidate_id": candidate_id,
            "operator_class": basin_id,
            "operator_directive": basin.directive,
            "basin_id": basin_id,
            "mechanism_signature": basin.mechanism_signature,
            "inherited_pivot_bnb_removed": True,
            "profiling_contract": self.r4_policy.required_profile_metrics,
            "planner_arm": self.planner_arm.value,
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
        slot = _candidate_slot(candidate_id)
        contrarian = slot in next(
            item.contrarian_slots
            for item in self.m3_policy.arms
            if item.arm is self.planner_arm
        )
        properties["contrarian_thesis"] = (
            {"type": "string", "minLength": 12}
            if contrarian
            else {"type": "null", "const": None}
        )
        operator_dir = plan_path.parent
        schema_path = operator_dir / f"{candidate_id}.escape_plan.schema.json"
        atomic_write_json(schema_path, schema)
        raw_path = operator_dir / f"{candidate_id}.escape_plan.raw.json"
        prompt = (
            "You are the EvidenceEvolve M3 structural-escape planner. Return the assigned "
            "mechanism and the machine-checkable mechanism-first contract. Useful interface "
            "knowledge may be preserved, but the inherited solver implementation, fallback, "
            "state representation, and branch/bound pipeline must not be retained.\n"
            + planner_treatment_prompt(self.m3_policy, self.planner_arm, slot)
            + "\nAssigned basin:\n"
            + basin.model_dump_json(indent=2)
            + "\nFrozen failure context:\n"
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
            raise RuntimeError(f"M3 planning failed: {candidate_id}")
        plan = M3StructuralEscapePlan.model_validate_json(
            raw_path.read_text(encoding="utf-8")
        )
        if plan.candidate_id != candidate_id or plan.basin_id != basin.basin_id:
            raise ValueError("M3 plan identity or basin drift")
        if (
            plan.operator_class != basin.basin_id
            or plan.operator_directive != basin.directive
        ):
            raise ValueError("M3 plan operator drift")
        if set(plan.mechanism_signature) != set(basin.mechanism_signature):
            raise ValueError("M3 plan mechanism signature drift")
        if set(plan.profiling_contract) != set(self.r4_policy.required_profile_metrics):
            raise ValueError("M3 plan profiling contract drift")
        if plan.genetic_parent_id not in parents:
            raise ValueError("M3 plan selected an ineligible parent")
        if plan.planner_arm is not self.planner_arm:
            raise ValueError("M3 planner arm drift")
        if contrarian != bool(plan.contrarian_thesis):
            raise ValueError("M3 contrarian allocation drift")
        create_once_json(plan_path, plan)
        decision = check_mechanism_contract(plan.mechanism_first(), self.m3_policy)
        create_once_json(
            operator_dir / f"{candidate_id}.mechanism_contract.json", decision
        )
        raw_path.unlink(missing_ok=True)
        if not decision.passed:
            raise ValueError(
                "M3 mechanism-first proposal blocked: " + ",".join(decision.reason_codes)
            )
        return plan

    def _load_assigned_plan(
        self, candidate_id: str, generation_id: str
    ) -> R4StructuralEscapePlan:
        if self.planner_arm is PlannerArm.R4_BASELINE:
            path = (
                self.run_dir
                / "generations"
                / generation_id
                / "r4_operator"
                / f"{candidate_id}.escape_plan.json"
            )
            return R4StructuralEscapePlan.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        return M3StructuralEscapePlan.model_validate_json(
            self._m3_plan_path(candidate_id, generation_id).read_text(encoding="utf-8")
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
        if self.planner_arm is PlannerArm.R4_BASELINE:
            return super()._proposal_prompt(
                candidate_id=candidate_id,
                generation_id=generation_id,
                island=island,
                eligible_parents=eligible_parents,
                feedback=feedback,
                required_mutation=required_mutation,
                research_action=research_action,
                mode=mode,
            )
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
        plan = self._load_assigned_plan(candidate_id, generation_id)
        return (
            prompt
            + "\nThe M3 mechanism-first plan below is frozen. Translate it faithfully into "
            "the CandidateGenome; do not weaken deleted components into optional fallbacks.\n"
            + plan.model_dump_json(indent=2)
        )

    def _proposal_schema(
        self,
        candidate_id: str,
        island: str,
        eligible_parents: list[str],
        required_mutation: MutationType,
    ) -> dict[str, object]:
        schema = M2R2AutonomousCampaignRunner._proposal_schema(
            self, candidate_id, island, eligible_parents, required_mutation
        )
        plan = self._load_assigned_plan(candidate_id, candidate_id.rsplit("-C", 1)[0])
        schema["$defs"]["CandidateGenome"]["properties"]["family"] = {
            "const": plan.basin_id,
            "type": "string",
        }
        return schema

    def _validate_proposal_vocabulary(self, candidate: CampaignCandidate) -> None:
        M2AutonomousCampaignRunner._validate_proposal_vocabulary(self, candidate)
        genome = candidate.acquisition.candidate
        generation_id = genome.candidate_id.rsplit("-C", 1)[0]
        plan = self._load_assigned_plan(genome.candidate_id, generation_id)
        if genome.genetic_parent_id != plan.genetic_parent_id:
            raise ValueError("M3 proposal ignored its frozen interface parent")
        if genome.family.strip().casefold() != plan.basin_id.strip().casefold():
            raise ValueError("M3 proposal ignored its frozen structural basin")

    def _implementation_prompt(self, item: CampaignCandidate) -> str:
        prompt = M2AutonomousCampaignRunner._implementation_prompt(self, item)
        candidate_id = item.acquisition.candidate.candidate_id
        generation_id = candidate_id.rsplit("-C", 1)[0]
        plan = self._load_assigned_plan(candidate_id, generation_id)
        return (
            prompt
            + "\nImplementation fidelity is an L0 hard contract. Delete every component "
            "declared deleted below; calling or copying the closed solver under any name is "
            "STRUCTURAL_ESCAPE_VIOLATION. Implement profile_snapshot().\n"
            + plan.model_dump_json(indent=2)
        )


class _FingerprintNormalizer(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Constant(value=None), node)

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Name(id="_name", ctx=node.ctx), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
        return ast.copy_location(
            ast.Attribute(value=self.visit(node.value), attr="_attr", ctx=node.ctx), node
        )

    def visit_arg(self, node: ast.arg) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.arg(arg="_arg", annotation=None), node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
        updated = self.generic_visit(node)
        updated.name = "_function"
        updated.decorator_list = []
        updated.returns = None
        return updated


def _definitions(tree: ast.AST) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _fingerprint(node: ast.AST) -> str:
    normalized = _FingerprintNormalizer().visit(copy.deepcopy(node))
    ast.fix_missing_locations(normalized)
    payload = ast.dump(normalized, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identifiers(tree: ast.AST) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            values.add(node.name.casefold())
        elif isinstance(node, ast.Name):
            values.add(node.id.casefold())
        elif isinstance(node, ast.Attribute):
            values.add(node.attr.casefold())
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module.casefold())
    return values


def _call_graph(tree: ast.AST) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls: set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                calls.add(child.func.id.casefold())
            elif isinstance(child.func, ast.Attribute):
                calls.add(child.func.attr.casefold())
        graph[node.name.casefold()] = sorted(calls)
    return dict(sorted(graph.items()))


def _reachable(graph: dict[str, list[str]], root: str = "solve") -> list[str]:
    pending = [root]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(name for name in graph.get(current, []) if name not in seen)
    return sorted(seen)


class MechanismAncestryReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    direct_closed_markers: list[str]
    matched_closed_core_fingerprints: dict[str, str]
    solver_reachable_symbols: list[str]
    reachable_closed_markers: list[str]
    observed_state_markers: list[str]
    observed_pipeline_markers: list[str]
    state_pipeline_lineage_match: bool = False
    lineage_retained: bool
    structural_escape_pass: bool
    reason_codes: list[str]
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )


class MechanismAncestryDetector:
    """Detect direct and renamed retention of a declared closed solver core."""

    def __init__(
        self,
        *,
        policy: M3ResearchTastePolicy,
        closed_source: Path,
        report_dir: Path | None = None,
    ) -> None:
        self.policy = policy
        self.closed_source = closed_source
        self.report_dir = report_dir
        tree = ast.parse(closed_source.read_text(encoding="utf-8"))
        definitions = _definitions(tree)
        missing = [name for name in policy.closed_core_symbols if name not in definitions]
        if missing:
            raise ValueError(f"closed source lacks configured core symbols: {missing}")
        self._closed_fingerprints = {
            name: _fingerprint(definitions[name]) for name in policy.closed_core_symbols
        }

    def assess(self, candidate_id: str, candidate_source: Path) -> MechanismAncestryReport:
        source = candidate_source.read_text(encoding="utf-8")
        tree = ast.parse(source)
        identifiers = _identifiers(tree)
        definitions = _definitions(tree)
        graph = _call_graph(tree)
        reachable = _reachable(graph)
        direct = sorted(
            marker
            for marker in self.policy.closed_source_markers
            if marker.casefold() in identifiers
        )
        matched: dict[str, str] = {}
        for candidate_name, node in definitions.items():
            fingerprint = _fingerprint(node)
            for closed_name, closed_fingerprint in self._closed_fingerprints.items():
                if fingerprint == closed_fingerprint:
                    matched[candidate_name] = closed_name
        reachable_closed = sorted(
            marker
            for marker in self.policy.closed_source_markers
            if marker.casefold() in reachable
        )
        state = sorted(
            marker
            for marker in self.policy.state_representation_markers
            if marker.casefold() in identifiers
        )
        pipeline = sorted(
            marker
            for marker in self.policy.pipeline_markers
            if marker.casefold() in identifiers
        )
        state_pipeline_match = bool(
            len(state) >= self.policy.state_pipeline_min_state_markers
            and len(pipeline) >= self.policy.state_pipeline_min_pipeline_markers
        )
        retained = bool(direct or matched or reachable_closed or state_pipeline_match)
        reasons = (
            ["STRUCTURAL_ESCAPE_VIOLATION"]
            if retained
            else ["MECHANISM_ANCESTRY_CLEAR"]
        )
        report = MechanismAncestryReport(
            candidate_id=candidate_id,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            direct_closed_markers=direct,
            matched_closed_core_fingerprints=dict(sorted(matched.items())),
            solver_reachable_symbols=reachable,
            reachable_closed_markers=reachable_closed,
            observed_state_markers=state,
            observed_pipeline_markers=pipeline,
            state_pipeline_lineage_match=state_pipeline_match,
            lineage_retained=retained,
            structural_escape_pass=not retained,
            reason_codes=reasons,
        )
        if self.report_dir is not None:
            atomic_write_json(
                self.report_dir / f"{candidate_id}.mechanism_ancestry.json", report
            )
        return report

    def __call__(self, ticket: object, item: object) -> bool:
        candidate_id = str(getattr(ticket, "candidate_id"))
        worktree = Path(getattr(item, "worktree"))
        source = worktree / "tasks/algotune_set_cover/initial.py"
        return self.assess(candidate_id, source).structural_escape_pass

    def root_key(self, ticket: object, item: object) -> str | None:
        if not self(ticket, item):
            return None
        family = getattr(item, "item").acquisition.candidate.family
        safe = re.sub(r"[^a-z0-9]+", "_", family.casefold()).strip("_")
        return f"m3_mechanism_{safe}"


class M3StructuralEscapeStagedAdapter:
    """L0 wrapper that makes ancestry and telemetry non-bypassable hard gates.

    The wrapped profiled adapter still owns evaluation and L1/L2 behavior.  M3
    changes only L0 scheduling: any ancestry, exactness, or telemetry failure is
    an invalid mechanics/adapter outcome, never a fifth scientific outcome.
    """

    def __init__(self, *, profiled_adapter: object, detector: MechanismAncestryDetector):
        self.profiled_adapter = profiled_adapter
        self.detector = detector
        self.policy = getattr(profiled_adapter, "policy")

    def l0(self, ticket: object, item: object) -> object:
        from evidence_evolve.discovery.throughput import (
            FunnelDecision,
            FunnelStage,
            StageStatus,
        )
        from evidence_evolve.models import MechanicsStatus

        candidate_path = self.profiled_adapter.candidate_path(item)
        ancestry = self.detector.assess(str(getattr(ticket, "candidate_id")), candidate_path)
        if not ancestry.structural_escape_pass:
            return FunnelDecision(
                stage=FunnelStage.L0,
                status=StageStatus.BLOCK,
                continue_pipeline=False,
                mechanics_status=MechanicsStatus.FAIL,
                data_eligible=False,
                controls={
                    "candidate_valid": False,
                    "synthetic_only": True,
                    "profile_contract_complete": False,
                    "closed_lineage_absent": False,
                },
                metrics={
                    "mechanics_valid_rate": 0.0,
                    "mechanics_elapsed_seconds": 0.0,
                    "evaluator_worker_count": 0.0,
                },
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reason_codes=[
                    "M3_L0_ADMISSION_BLOCK",
                    "SYNTHETIC_MECHANICS_NOT_RUN_LINEAGE_BLOCK",
                    *ancestry.reason_codes,
                ],
            )
        raw = self.profiled_adapter._run(  # noqa: SLF001
            item,
            seeds=self.policy.mechanics_seeds,
            repeats=self.policy.mechanics_repeats,
        )
        exact = bool(raw.get("correct")) and not bool(raw.get("adapter_exception"))
        telemetry = bool(raw.get("telemetry_available"))
        passed = exact and telemetry
        reasons = ["M3_L0_ADMISSION_PASS" if passed else "M3_L0_ADMISSION_BLOCK"]
        if not exact:
            reasons.append("SYNTHETIC_MECHANICS_FAIL")
        if not telemetry:
            reasons.append("PROFILE_CONTRACT_INCOMPLETE")
        reasons.extend(ancestry.reason_codes)
        failure_reason = self.profiled_adapter._failure_reason(raw)  # noqa: SLF001
        if failure_reason:
            reasons.append(failure_reason)
        profile_metrics = self.profiled_adapter._profile_metrics(raw)  # noqa: SLF001
        return FunnelDecision(
            stage=FunnelStage.L0,
            status=StageStatus.PASS if passed else StageStatus.BLOCK,
            continue_pipeline=passed,
            mechanics_status=MechanicsStatus.PASS if exact else MechanicsStatus.FAIL,
            data_eligible=False,
            controls={
                "candidate_valid": exact,
                "synthetic_only": True,
                "profile_contract_complete": telemetry,
                "closed_lineage_absent": ancestry.structural_escape_pass,
            },
            metrics={
                "mechanics_valid_rate": float(raw.get("valid_rate", 0.0)),
                "mechanics_elapsed_seconds": float(raw["elapsed_seconds"]),
                "evaluator_worker_count": float(raw.get("worker_count", 1)),
                **profile_metrics,
            },
            scientific_outcome=(
                ScientificOutcome.NOT_EVALUABLE_DATA
                if passed
                else ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
            ),
            reason_codes=list(dict.fromkeys(reasons)),
        )

    def l1(self, ticket: object, item: object, l0: object) -> object:
        return self.profiled_adapter.l1(ticket, item, l0)

    def full_evaluation(self, item: object) -> object:
        return self.profiled_adapter.full_evaluation(item)

    def promotion_worthy(self, evaluation: object) -> bool:
        return bool(self.profiled_adapter.promotion_worthy(evaluation))

    def structural_transition_pass(self, ticket: object, item: object) -> bool:
        return bool(getattr(ticket, "requires_structural_transition", False)) and bool(
            self.detector(ticket, item)
        )

    def structural_root_key(self, ticket: object, item: object) -> str | None:
        if not getattr(ticket, "requires_structural_transition", False):
            return None
        return self.detector.root_key(ticket, item)


class M3CandidateObservation(StrictModel):
    candidate_id: str
    arm: PlannerArm
    paired_slot: int = Field(ge=1, le=8)
    proposal_contract_pass: bool | None = None
    implementation_succeeded: bool
    lineage_retained: bool | None = None
    exact_valid: bool | None = None
    basin_signature: list[str] = Field(default_factory=list)
    token_count: int = Field(ge=0)
    scientific_outcome: ScientificOutcome
    raw_speedup: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def downstream_facts_require_implementation(self) -> "M3CandidateObservation":
        if not self.implementation_succeeded and (
            self.lineage_retained is not None or self.exact_valid is not None
        ):
            raise ValueError("lineage and exactness require an implementation")
        if self.arm is PlannerArm.R4_BASELINE and self.proposal_contract_pass is not None:
            raise ValueError("baseline did not receive the mechanism-first contract")
        if self.arm is not PlannerArm.R4_BASELINE and self.proposal_contract_pass is None:
            raise ValueError("treatment arms require a proposal-contract decision")
        return self


class M3ArmSummary(StrictModel):
    arm: PlannerArm
    proposals: int
    proposal_contract_passes: int | None
    implementations: int
    genuine_structural_roots: int
    inherited_solver_retentions: int
    exact_valid_roots: int
    distinct_basin_signatures: int
    tokens: int
    genuine_roots_per_million_tokens: float
    best_exact_valid_speedup: float | None
    admission_target_met: bool


class ResearchTasteScore(StrictModel):
    candidate_id: str
    assumption_removal: float = Field(ge=0.0, le=1.0)
    primitive_novelty: float = Field(ge=0.0, le=1.0)
    implementation_fidelity: float = Field(ge=0.0, le=1.0)
    failure_information_gain: float = Field(ge=0.0, le=1.0)
    direction_correction: float = Field(ge=0.0, le=1.0)
    scheduling_score: float = Field(ge=0.0, le=1.0)
    performance_excluded: Literal[True] = True
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )


def score_research_taste(
    plan: MechanismFirstPlan,
    ancestry: MechanismAncestryReport,
    *,
    prior_primitive_signatures: list[list[str]],
) -> ResearchTasteScore:
    """A scheduling reward for direction quality, explicitly excluding speedup."""

    proposed = {item.strip().casefold() for item in plan.new_computational_primitives}
    similarities: list[float] = []
    for signature in prior_primitive_signatures:
        prior = {item.strip().casefold() for item in signature}
        union = proposed | prior
        similarities.append(0.0 if not union else len(proposed & prior) / len(union))
    components = {
        "assumption_removal": min(1.0, len(plan.removed_assumptions) / 3.0),
        "primitive_novelty": 1.0 - max(similarities, default=0.0),
        "implementation_fidelity": 0.0 if ancestry.lineage_retained else 1.0,
        "failure_information_gain": 1.0,
        "direction_correction": 1.0 if plan.contrarian_thesis else 0.0,
    }
    return ResearchTasteScore(
        candidate_id=plan.candidate_id,
        **components,
        scheduling_score=sum(components.values()) / len(components),
    )


def summarize_m3(
    observations: list[M3CandidateObservation], policy: M3ResearchTastePolicy
) -> list[M3ArmSummary]:
    """Summarize planner capability; speedup is deliberately a secondary endpoint."""

    summaries: list[M3ArmSummary] = []
    for arm in PlannerArm:
        rows = [item for item in observations if item.arm is arm]
        genuine = [
            item
            for item in rows
            if item.implementation_succeeded and item.lineage_retained is False
        ]
        exact = [item for item in genuine if item.exact_valid is True]
        signatures = {tuple(item.basin_signature) for item in genuine if item.basin_signature}
        tokens = sum(item.token_count for item in rows)
        contract_passes = (
            None
            if arm is PlannerArm.R4_BASELINE
            else sum(item.proposal_contract_pass is True for item in rows)
        )
        summaries.append(
            M3ArmSummary(
                arm=arm,
                proposals=len(rows),
                proposal_contract_passes=contract_passes,
                implementations=sum(item.implementation_succeeded for item in rows),
                genuine_structural_roots=len(genuine),
                inherited_solver_retentions=sum(
                    item.lineage_retained is True for item in rows
                ),
                exact_valid_roots=len(exact),
                distinct_basin_signatures=len(signatures),
                tokens=tokens,
                genuine_roots_per_million_tokens=(
                    0.0 if tokens == 0 else len(genuine) * 1_000_000.0 / tokens
                ),
                best_exact_valid_speedup=max(
                    (item.raw_speedup for item in exact if item.raw_speedup is not None),
                    default=None,
                ),
                admission_target_met=(
                    len(rows) == 8 and len(genuine) >= policy.admission_target
                ),
            )
        )
    return summaries


def load_m3_policy(path: Path) -> M3ResearchTastePolicy:
    import yaml

    return M3ResearchTastePolicy.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


__all__ = [
    "M3ArmSummary",
    "M3CandidateObservation",
    "M3AutonomousCampaignRunner",
    "M3ResearchTastePolicy",
    "M3StructuralEscapeStagedAdapter",
    "M3StructuralEscapePlan",
    "MechanismAncestryDetector",
    "MechanismAncestryReport",
    "MechanismContractDecision",
    "MechanismFirstPlan",
    "NegativeTasteRule",
    "PlannerArm",
    "PlannerArmSpec",
    "ResearchTasteScore",
    "check_mechanism_contract",
    "load_m3_policy",
    "planner_treatment_prompt",
    "score_research_taste",
    "summarize_m3",
]
