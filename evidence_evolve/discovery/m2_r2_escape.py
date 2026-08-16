"""M2-R2 context-preserving, failure-directed structural escape operator.

R1B assets remain immutable.  This version adds a deterministic development-only
failure packet and a separate structured mechanism-substitution plan before the
normal CandidateGenome proposal call.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.backends.codex_cli import CodexRole
from evidence_evolve.discovery.autonomous import _codex_output_schema
from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.discovery.m2_escape import (
    M2AutonomousCampaignRunner,
    M2ControllerTrace,
    M2EscapeCampaignController,
    M2EscapePolicy,
)
from evidence_evolve.governance.protocol_lock import load_contract
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode
from evidence_evolve.models import (
    EvidencePermission,
    MechanicsStatus,
    MutationType,
    ObjectiveDirection,
    ScientificOutcome,
    StrictModel,
)


class FailureClass(StrEnum):
    NO_EVALUATION = "NO_EVALUATION"
    INVALID_MECHANICS = "INVALID_MECHANICS"
    PROTOCOL_OR_CONTROL_FAILURE = "PROTOCOL_OR_CONTROL_FAILURE"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    BELOW_INCUMBENT = "BELOW_INCUMBENT"
    AT_OR_ABOVE_INCUMBENT = "AT_OR_ABOVE_INCUMBENT"


class M2R2Policy(M2EscapePolicy):
    """A new policy version; never retrofit into an R1 campaign."""

    structural_operator: Literal[
        "failure_directed_mechanism_substitution"
    ] = "failure_directed_mechanism_substitution"
    failure_window: int = Field(default=8, ge=2, le=64)
    require_non_seed_parent: Literal[True] = True
    require_cross_lineage_context: bool = False
    operator_plan_calls_per_escape: Literal[1] = 1

    @model_validator(mode="after")
    def restart_is_not_an_r2_escape(self) -> "M2R2Policy":
        if self.force_seed_restart_roots:
            raise ValueError("M2-R2 preserves context and cannot force SEED roots")
        if self.breakthrough_mutation_mix.get(MutationType.RESTART, 0.0) > 0.0:
            raise ValueError("M2-R2 breakthrough mix cannot allocate restart")
        required = {MutationType.CROSS_FAMILY, MutationType.FAILURE_DIRECTED}
        active = {
            mutation
            for mutation, weight in self.breakthrough_mutation_mix.items()
            if weight > 0.0
        }
        if active != required:
            raise ValueError(
                "M2-R2 breakthrough mix must isolate cross-family and "
                "failure-directed substitutions"
            )
        return self


class FailureObservation(StrictModel):
    candidate_id: str
    family: str
    mutation_type: MutationType
    genetic_parent_id: str
    parent_ids: list[str]
    hypothesis: str
    intervention: str
    mechanism_claims: list[str]
    transfer_motifs: list[str]
    failure_risks: list[str]
    mechanics_status: MechanicsStatus
    data_eligible: bool
    controls: dict[str, bool] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    scientific_outcome: ScientificOutcome
    protocol_violations: list[str] = Field(default_factory=list)
    objective_value: float | None = None
    incumbent_gap: float | None = None
    failure_class: FailureClass
    mechanism_support: str | None = None
    mechanism_reasons: list[str] = Field(default_factory=list)
    patch_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    parent_patch_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class M2R2FailureModel(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    generation_id: str
    incumbent_metric: str
    incumbent_value: float
    eligible_parent_ids: list[str]
    observations: list[FailureObservation] = Field(min_length=1)
    failure_counts: dict[str, int]
    source_candidate_ids: list[str] = Field(min_length=1)
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    blind_artifacts_read: Literal[False] = False
    confirmation_artifacts_read: Literal[False] = False
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )


class StructuralEscapePlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    operator_class: str = "generic_structural_escape"
    operator_directive: str = "Produce a context-preserving structural escape."
    genetic_parent_id: str
    context_candidate_ids: list[str] = Field(min_length=1)
    addressed_failure_candidate_ids: list[str] = Field(min_length=1)
    preserved_mechanisms: list[str] = Field(min_length=1)
    mechanism_to_replace: str = Field(min_length=4)
    replacement_mechanism: str = Field(min_length=4)
    target_family: str = Field(min_length=2)
    representation_change: str = Field(min_length=4)
    solver_process_change: str = Field(min_length=4)
    integration_steps: list[str] = Field(min_length=1)
    correctness_invariants: list[str] = Field(min_length=1)
    predicted_failure_mode: str = Field(min_length=4)
    falsifier: str = Field(min_length=4)
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )

    @model_validator(mode="after")
    def substitution_is_not_a_rename(self) -> "StructuralEscapePlan":
        if self.mechanism_to_replace.strip().casefold() == (
            self.replacement_mechanism.strip().casefold()
        ):
            raise ValueError("replacement mechanism must differ from the inherited one")
        return self


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class M2R2AutonomousCampaignRunner(M2AutonomousCampaignRunner):
    """Compile failure evidence and a substitution plan before each R2 escape."""

    def __init__(
        self,
        *args: Any,
        r2_policy: M2R2Policy,
        context_run_dirs: list[Path] | None = None,
        **kwargs: Any,
    ) -> None:
        self._r2_context_run_dirs = tuple(
            sorted(
                (path.resolve() for path in (context_run_dirs or [])),
                key=lambda path: path.as_posix(),
            )
        )
        self._validate_context_runs()
        super().__init__(*args, **kwargs)
        if self.policy != r2_policy.frozen_base_policy():
            raise ValueError("runner base policy does not match M2-R2 policy")
        self.r2_policy = r2_policy
        self._r2_artifact_lock = threading.Lock()
        self._bind_context_runs()

    def _validate_context_runs(self) -> None:
        prohibited = {EvidencePermission.CONFIRM, EvidencePermission.CLAIM}
        for run_dir in self._r2_context_run_dirs:
            contract_path = run_dir / "contract.locked.yaml"
            manifest_path = run_dir / "run_manifest.json"
            if not contract_path.is_file() or not manifest_path.is_file():
                raise ValueError(f"M2-R2 context run is incomplete: {run_dir}")
            contract = load_contract(contract_path)
            if contract.lock is None:
                raise ValueError(f"M2-R2 context contract is not locked: {run_dir}")
            if contract.budgets.confirmation_runs != 0:
                raise ValueError("M2-R2 context run allocated confirmation budget")
            if contract.authority.confirmation_visible_to_agents:
                raise ValueError("M2-R2 context exposed confirmation to agents")
            permissions = {
                permission
                for source in contract.evidence_sources
                for permission in source.permissions
            }
            if permissions & prohibited:
                raise ValueError(
                    "M2-R2 context run contains CONFIRM/CLAIM evidence permissions"
                )
            if "DEVELOPMENT_ONLY" not in contract.campaign.claim_scope:
                raise ValueError("M2-R2 context run is not development-only")

    def _bind_context_runs(self) -> None:
        payload = {
            "schema_version": "1.0",
            "context_runs": [
                {
                    "path": str(path),
                    "contract_sha256": load_contract(
                        path / "contract.locked.yaml"
                    ).lock.content_sha256,
                    "manifest_sha256": sha256_file(path / "run_manifest.json"),
                }
                for path in self._r2_context_run_dirs
            ],
            "context_sha256": sha256_object(
                [str(path) for path in self._r2_context_run_dirs]
            ),
            "evidence_scope": "DEVELOPMENT_ONLY",
            "scientific_authority": "NONE_SCHEDULING_ONLY",
            "blind_artifacts_read": False,
            "confirmation_artifacts_read": False,
        }
        path = self.run_dir / "m2_r2_context_manifest.json"
        if path.exists():
            if _read_json(path) != payload:
                raise ValueError("M2-R2 context manifest drift")
        else:
            create_once_json(path, payload)

    @staticmethod
    def _generation_id(candidate_id: str) -> str:
        marker = candidate_id.rfind("-C")
        if marker <= 0:
            raise ValueError(f"candidate id has no generation prefix: {candidate_id}")
        return candidate_id[:marker]

    def _controller_state(self, generation_id: str) -> M2ControllerTrace:
        path = (
            self.run_dir
            / "generations"
            / generation_id
            / "m2_controller_state.json"
        )
        return M2ControllerTrace.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def _proposal_payload(self, candidate_id: str) -> dict[str, Any] | None:
        generation_id = self._generation_id(candidate_id)
        for run_dir in (self.run_dir, *self._r2_context_run_dirs):
            path = (
                run_dir
                / "generations"
                / generation_id
                / "proposals"
                / f"{candidate_id}.json"
            )
            if path.is_file():
                return _read_json(path)
        return None

    def _receipt_paths(self, candidate_id: str) -> list[Path]:
        for run_dir in (self.run_dir, *self._r2_context_run_dirs):
            paths = sorted(
                (run_dir / "candidates" / candidate_id / "receipts").glob(
                    "*.M0_MECHANICS.json"
                )
            )
            if paths:
                return paths
        return []

    def _observation(
        self,
        candidate_id: str,
        *,
        incumbent_value: float,
    ) -> FailureObservation | None:
        proposal = self._proposal_payload(candidate_id)
        if proposal is None:
            return None
        genome = proposal["acquisition"]["candidate"]
        receipt_paths = self._receipt_paths(candidate_id)
        evaluation: dict[str, Any] | None = None
        receipt: dict[str, Any] | None = None
        if receipt_paths:
            receipt = _read_json(receipt_paths[0])["receipt"]
            evaluation = receipt["evaluation_input"]

        if evaluation is None:
            mechanics = MechanicsStatus.NOT_RUN
            eligible = False
            controls: dict[str, bool] = {}
            metrics: dict[str, float] = {}
            outcome = ScientificOutcome.NOT_EVALUABLE_DATA
            violations: list[str] = []
            failure_class = FailureClass.NO_EVALUATION
        else:
            mechanics = MechanicsStatus(str(evaluation["mechanics_status"]))
            eligible = bool(evaluation["data_eligible"])
            controls = {
                str(key): bool(value)
                for key, value in evaluation.get("controls", {}).items()
            }
            metrics = {
                str(key): float(value)
                for key, value in evaluation.get("metrics", {}).items()
            }
            raw_outcome = evaluation.get("scientific_outcome")
            outcome = (
                ScientificOutcome(str(raw_outcome))
                if raw_outcome is not None
                else ScientificOutcome.NOT_EVALUABLE_DATA
            )
            violations = [str(item) for item in evaluation.get("protocol_violations", [])]
            if mechanics is MechanicsStatus.FAIL or (
                outcome is ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
            ):
                failure_class = FailureClass.INVALID_MECHANICS
            elif violations or not controls or not all(controls.values()):
                failure_class = FailureClass.PROTOCOL_OR_CONTROL_FAILURE
            elif not eligible or outcome is ScientificOutcome.NOT_EVALUABLE_DATA:
                failure_class = FailureClass.NOT_EVALUABLE
            else:
                failure_class = FailureClass.BELOW_INCUMBENT

        objective = metrics.get(self.r2_policy.incumbent_metric)
        gap = None
        if objective is not None:
            direction = self.contract.metrics.pareto_objectives[
                self.r2_policy.incumbent_metric
            ]
            gap = (
                objective - incumbent_value
                if direction is ObjectiveDirection.MAXIMIZE
                else incumbent_value - objective
            )
            if (
                mechanics is MechanicsStatus.PASS
                and eligible
                and controls
                and all(controls.values())
                and not violations
                and gap >= 0.0
            ):
                failure_class = FailureClass.AT_OR_ABOVE_INCUMBENT

        mechanism_support = None
        mechanism_reasons: list[str] = []
        if receipt_paths:
            mechanism_path = receipt_paths[0].with_suffix(".mechanism.json")
            if mechanism_path.is_file():
                mechanism = _read_json(mechanism_path)
                mechanism_support = str(mechanism["support"])
                mechanism_reasons = [str(item) for item in mechanism.get("reasons", [])]
        return FailureObservation(
            candidate_id=candidate_id,
            family=str(genome["family"]),
            mutation_type=MutationType(str(genome["mutation_type"])),
            genetic_parent_id=str(genome["genetic_parent_id"]),
            parent_ids=[str(item) for item in genome["parent_ids"]],
            hypothesis=str(genome["hypothesis"]),
            intervention=str(genome["intervention"]),
            mechanism_claims=[str(item) for item in genome.get("mechanism_claims", [])],
            transfer_motifs=[str(item) for item in genome.get("transfer_motifs", [])],
            failure_risks=[str(item) for item in genome.get("failure_risks", [])],
            mechanics_status=mechanics,
            data_eligible=eligible,
            controls=controls,
            metrics=metrics,
            scientific_outcome=outcome,
            protocol_violations=violations,
            objective_value=objective,
            incumbent_gap=gap,
            failure_class=failure_class,
            mechanism_support=mechanism_support,
            mechanism_reasons=mechanism_reasons,
            patch_sha256=(
                str(receipt["patch_sha256"])
                if receipt and receipt.get("patch_sha256")
                else None
            ),
            parent_patch_sha256=(
                str(receipt["parent_patch_sha256"])
                if receipt and receipt.get("parent_patch_sha256")
                else None
            ),
        )

    def _failure_model(
        self,
        *,
        candidate_id: str,
        generation_id: str,
        eligible_parents: list[str],
    ) -> M2R2FailureModel:
        path = (
            self.run_dir
            / "generations"
            / generation_id
            / "r2_operator"
            / f"{candidate_id}.failure_model.json"
        )
        if path.is_file():
            return M2R2FailureModel.model_validate_json(path.read_text(encoding="utf-8"))
        state = self._controller_state(generation_id)
        prior_paths = sorted(
            (
                path
                for run_dir in (self.run_dir, *self._r2_context_run_dirs)
                for path in run_dir.glob("generations/*/proposals/*.json")
            ),
            key=lambda item: item.as_posix(),
        )
        prior_ids = [
            path.stem
            for path in prior_paths
            if path.parents[1].name < generation_id
        ]
        selected: list[str] = []
        for item in [
            *[parent for parent in eligible_parents if parent != "SEED"],
            *reversed(prior_ids),
        ]:
            if item not in selected:
                selected.append(item)
            if len(selected) >= self.r2_policy.failure_window:
                break
        observations = [
            observation
            for item in selected
            if (observation := self._observation(
                item,
                incumbent_value=state.incumbent_value_before,
            ))
            is not None
        ]
        if not observations:
            raise ValueError("M2-R2 escape has no development context to preserve")
        model = M2R2FailureModel(
            candidate_id=candidate_id,
            generation_id=generation_id,
            incumbent_metric=self.r2_policy.incumbent_metric,
            incumbent_value=state.incumbent_value_before,
            eligible_parent_ids=[parent for parent in eligible_parents if parent != "SEED"],
            observations=observations,
            failure_counts=dict(
                sorted(Counter(item.failure_class.value for item in observations).items())
            ),
            source_candidate_ids=[item.candidate_id for item in observations],
        )
        with self._r2_artifact_lock:
            if path.is_file():
                existing = M2R2FailureModel.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if existing != model:
                    raise ValueError("M2-R2 failure model drift")
            else:
                create_once_json(path, model)
        return model

    def _escape_plan(
        self,
        *,
        candidate_id: str,
        generation_id: str,
        eligible_parents: list[str],
        operator_contract: dict[str, str] | None = None,
    ) -> StructuralEscapePlan:
        operator_dir = (
            self.run_dir / "generations" / generation_id / "r2_operator"
        )
        operator_class = (
            operator_contract.get("operator_class", "generic_structural_escape")
            if operator_contract
            else "generic_structural_escape"
        )
        operator_directive = (
            operator_contract.get(
                "operator_directive",
                "Produce a context-preserving structural escape.",
            )
            if operator_contract
            else "Produce a context-preserving structural escape."
        )
        plan_path = operator_dir / f"{candidate_id}.escape_plan.json"
        if plan_path.is_file():
            existing = StructuralEscapePlan.model_validate_json(
                plan_path.read_text(encoding="utf-8")
            )
            if (
                existing.candidate_id != candidate_id
                or existing.operator_class != operator_class
                or existing.operator_directive != operator_directive
            ):
                raise ValueError("M2-R2 existing escape plan operator drift")
            return existing
        parents = [parent for parent in eligible_parents if parent != "SEED"]
        if not parents:
            raise ValueError("M2-R2 escape requires an evaluated non-SEED parent")
        failure_model = self._failure_model(
            candidate_id=candidate_id,
            generation_id=generation_id,
            eligible_parents=parents,
        )
        self.budgets.reserve(
            "proposal_calls",
            1,
            f"operator_plan_calls:{generation_id}:{candidate_id}",
        )
        schema = _codex_output_schema(StructuralEscapePlan.model_json_schema())
        properties = schema["properties"]
        properties["candidate_id"] = {"const": candidate_id, "type": "string"}
        properties["operator_class"] = {
            "const": operator_class,
            "type": "string",
        }
        properties["operator_directive"] = {
            "const": operator_directive,
            "type": "string",
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
            "You are the EvidenceEvolve M2-R2 structural escape planner. Return one "
            "StructuralEscapePlan matching the JSON Schema. Preserve useful implementation "
            "context, identify a concrete incumbent mechanism to replace, and select a "
            "different solver mechanism that directly addresses the deterministic failure "
            "model. Parameter tuning, cleanup, cache-only work, and SEED restart are "
            "prohibited. All supplied observations are development-only scheduling context, "
            "not scientific evidence. Do not access confirmation or blind assets.\n"
            f"Required candidate_id: {candidate_id}\n"
            f"Frozen operator class: {operator_class}\n"
            f"Frozen operator directive: {operator_directive}\n"
            f"Eligible genetic parents: {json.dumps(parents)}\n"
            "Deterministic failure model:\n"
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
            raise RuntimeError(f"M2-R2 escape planning failed for {candidate_id}")
        plan = StructuralEscapePlan.model_validate_json(
            raw_path.read_text(encoding="utf-8")
        )
        allowed = set(failure_model.source_candidate_ids)
        if plan.candidate_id != candidate_id:
            raise ValueError("M2-R2 plan candidate identity drift")
        if plan.operator_class != operator_class:
            raise ValueError("M2-R2 plan operator class drift")
        if plan.operator_directive != operator_directive:
            raise ValueError("M2-R2 plan operator directive drift")
        if plan.genetic_parent_id not in parents:
            raise ValueError("M2-R2 plan selected an ineligible genetic parent")
        if not set(plan.context_candidate_ids).issubset(allowed) or not set(
            plan.addressed_failure_candidate_ids
        ).issubset(allowed):
            raise ValueError("M2-R2 plan cited context outside the frozen failure model")
        if self.r2_policy.require_cross_lineage_context:
            state = self._controller_state(generation_id)
            parent_root = state.root_lineages.get(
                plan.genetic_parent_id, plan.genetic_parent_id
            )
            if not any(
                state.root_lineages.get(item, item) != parent_root
                for item in plan.context_candidate_ids
            ):
                raise ValueError(
                    "M2-R2 policy requires context from a distinct archived lineage"
                )
        create_once_json(plan_path, plan)
        raw_path.unlink(missing_ok=True)
        return plan

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
        if mode is DiscoveryMode.BREAKTHROUGH:
            raw_contract = feedback.get("async_operator_contract")
            operator_contract = (
                {
                    str(key): str(value)
                    for key, value in raw_contract.items()
                }
                if isinstance(raw_contract, dict)
                else None
            )
            self._escape_plan(
                candidate_id=candidate_id,
                generation_id=generation_id,
                eligible_parents=eligible_parents,
                operator_contract=operator_contract,
            )
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
        prompt = super()._proposal_prompt(
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
            return prompt
        operator_dir = self.run_dir / "generations" / generation_id / "r2_operator"
        failure_model = M2R2FailureModel.model_validate_json(
            (operator_dir / f"{candidate_id}.failure_model.json").read_text(
                encoding="utf-8"
            )
        )
        plan = StructuralEscapePlan.model_validate_json(
            (operator_dir / f"{candidate_id}.escape_plan.json").read_text(
                encoding="utf-8"
            )
        )
        return (
            prompt
            + "\nM2-R2 requires a context-preserving mechanism substitution. The "
            "CandidateGenome must use the plan's genetic parent and target family; include "
            "mechanism claims, inherited transfer motifs, failure risks, and an ablation "
            "that can falsify the substitution. SEED restart is prohibited.\n"
            "Frozen M2-R2 failure model:\n"
            + failure_model.model_dump_json(indent=2)
            + "\nFrozen M2-R2 structural escape plan:\n"
            + plan.model_dump_json(indent=2)
        )

    def _validate_proposal_vocabulary(self, candidate: CampaignCandidate) -> None:
        super()._validate_proposal_vocabulary(candidate)
        genome = candidate.acquisition.candidate
        generation_id = self._generation_id(genome.candidate_id)
        state = self._controller_state(generation_id)
        if state.mode is not DiscoveryMode.BREAKTHROUGH:
            return
        plan_path = (
            self.run_dir
            / "generations"
            / generation_id
            / "r2_operator"
            / f"{genome.candidate_id}.escape_plan.json"
        )
        plan = StructuralEscapePlan.model_validate_json(
            plan_path.read_text(encoding="utf-8")
        )
        if genome.genetic_parent_id == "SEED":
            raise ValueError("M2-R2 proposal discarded accumulated implementation context")
        if genome.genetic_parent_id != plan.genetic_parent_id:
            raise ValueError("M2-R2 proposal ignored the frozen genetic parent")
        if genome.family.strip().casefold() != plan.target_family.strip().casefold():
            raise ValueError("M2-R2 proposal ignored the frozen target family")
        if not genome.mechanism_claims:
            raise ValueError("M2-R2 proposal omitted mechanism claims")
        if not genome.transfer_motifs:
            raise ValueError("M2-R2 proposal omitted preserved transfer motifs")
        if not genome.failure_risks:
            raise ValueError("M2-R2 proposal omitted predicted failure risks")
        if not genome.ablation_plan:
            raise ValueError("M2-R2 proposal omitted a substitution ablation")


class M2R2EscapeCampaignController(M2EscapeCampaignController):
    """R2 manifest and parent boundary over the repaired M2 controller."""

    policy: M2R2Policy

    def _bind_controller(self) -> None:
        payload = {
            "schema_version": "1.0",
            "controller": "M2R2EscapeCampaignController",
            "policy": self.policy.model_dump(mode="json"),
            "policy_sha256": hashlib.sha256(
                self.policy.model_dump_json().encode("utf-8")
            ).hexdigest(),
            "base_policy_sha256": hashlib.sha256(
                self.runner.policy.model_dump_json().encode("utf-8")
            ).hexdigest(),
            "stagnation_authority": "strict incumbent refresh only",
            "structural_root_definition": (
                "non-SEED inherited implementation plus frozen mechanism substitution "
                "plan; semantic novelty remains scheduling-only until deterministic "
                "structural/behavior checks run"
            ),
            "parent_rights_authority": (
                "all admitted archive members; active population eviction cannot remove "
                "incumbent parent rights"
            ),
            "operator_pipeline": [
                "DETERMINISTIC_FAILURE_MODEL",
                "STRUCTURAL_ESCAPE_PLAN",
                "CANDIDATE_PROPOSAL",
            ],
            "evidence_scope": "DEVELOPMENT_ONLY",
            "scientific_authority": "NONE_SCHEDULING_ONLY",
            "blind_artifacts_read": False,
            "confirmation_artifacts_read": False,
        }
        path = self.run_dir / "m2_r2_controller_manifest.json"
        if path.exists():
            if _read_json(path) != payload:
                raise ValueError("M2-R2 controller manifest drift")
        else:
            create_once_json(path, payload)

    def _parent_pool(
        self,
        *,
        state: dict[str, object],
        mode: DiscoveryMode,
        mutation: MutationType,
    ) -> tuple[list[str], list[str], list[str]]:
        parent_pool, preferred, admitted = super()._parent_pool(
            state=state,
            mode=mode,
            mutation=mutation,
        )
        if mode is DiscoveryMode.BREAKTHROUGH and (
            not parent_pool or "SEED" in parent_pool
        ):
            raise ValueError("M2-R2 cannot schedule structural escape without archive context")
        return parent_pool, preferred, admitted


__all__ = [
    "FailureClass",
    "FailureObservation",
    "M2R2AutonomousCampaignRunner",
    "M2R2EscapeCampaignController",
    "M2R2FailureModel",
    "M2R2Policy",
    "StructuralEscapePlan",
]
