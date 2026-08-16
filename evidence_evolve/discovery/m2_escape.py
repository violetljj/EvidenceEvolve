"""Versioned M2 escape controller layered over the frozen autonomous runner.

The existing controller, population store, models, and director remain byte-for-byte
unchanged for locked campaigns. This planner writes immutable per-generation
director/policy traces before delegating execution to AutonomousCampaignRunner.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.discovery.autonomous import (
    AutonomousCampaignResult,
    AutonomousCampaignRunner,
)
from evidence_evolve.discovery.director import ResearchAction, ResearchDirectorDecision
from evidence_evolve.meta_evolution.policy import (
    DiscoveryMode,
    PolicyEffectTrace,
    ResearchPolicyGenome,
    mutation_schedule,
)
from evidence_evolve.models import MutationType, ObjectiveDirection, StrictModel


class M2EscapePolicy(ResearchPolicyGenome):
    """New campaign policy; never retrofit into an already locked campaign."""

    stagnation_reset: Literal["incumbent_refresh"] = "incumbent_refresh"
    incumbent_metric: str
    incumbent_min_delta: float = Field(default=0.0, ge=0.0)
    parent_quality_guardrail_fraction: float = Field(default=0.005, ge=0.0, le=1.0)
    escape_budget_generations: int = Field(default=4, ge=1)
    force_seed_restart_roots: bool = False
    prefer_distinct_root_context: bool = True

    @model_validator(mode="after")
    def radical_roots_require_restart_budget(self) -> "M2EscapePolicy":
        if (
            self.force_seed_restart_roots
            and self.breakthrough_mutation_mix.get(MutationType.RESTART, 0.0) <= 0.0
        ):
            raise ValueError("forced SEED roots require non-zero restart allocation")
        return self

    def frozen_base_policy(self) -> ResearchPolicyGenome:
        payload = {
            name: getattr(self, name)
            for name in ResearchPolicyGenome.model_fields
        }
        return ResearchPolicyGenome.model_validate(payload)


class M2ControllerTrace(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    generation_id: str
    policy_id: str
    mode: DiscoveryMode
    incumbent_metric: str
    incumbent_value_before: float
    stagnant_generations_before: int = Field(ge=0)
    escape_budget_remaining_before: int = Field(ge=0)
    escape_triggered: bool
    mutation_assignment: MutationType
    parent_pool: list[str] = Field(min_length=1)
    preferred_parent_ids: list[str]
    admitted_parent_ids: list[str]
    objective_values: dict[str, float]
    root_lineages: dict[str, str]
    required_structural_transition: bool
    required_seed_root: bool
    evidence_scope: Literal["DEVELOPMENT_ONLY"] = "DEVELOPMENT_ONLY"
    blind_artifacts_read: Literal[False] = False


class M2AutonomousCampaignRunner(AutonomousCampaignRunner):
    """Prospective runner with an M2-only structural escape prompt extension."""

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
        state_path = (
            self.run_dir
            / "generations"
            / generation_id
            / "m2_controller_state.json"
        )
        state = M2ControllerTrace.model_validate_json(
            state_path.read_text(encoding="utf-8")
        )
        return (
            prompt
            + "\nM2 BREAKTHROUGH is a protected structural operator class. The proposal "
            "must change at least one of: mathematical formulation, solver family, "
            "problem representation, decomposition/reduction, or hybrid exact method. "
            "A parameter tweak, control-flow cleanup, simplification, cache-only edit, "
            "or failure-local patch is not sufficient. For restart, design from the "
            "SEED code without inheriting a prior candidate's implementation. Treat "
            "root lineage labels and objective values below as development-only "
            "scheduling context, never as scientific evidence.\nM2 controller state:\n"
            + state.model_dump_json(indent=2)
        )


class M2EscapeCampaignController:
    """Plan repaired controller state, then let frozen execution/gates decide."""

    def __init__(
        self,
        *,
        runner: AutonomousCampaignRunner,
        policy: M2EscapePolicy,
    ) -> None:
        if runner.policy != policy.frozen_base_policy():
            raise ValueError("runner base policy does not match M2 policy base fields")
        if policy.incumbent_metric not in runner.contract.metrics.pareto_objectives:
            raise ValueError(
                f"incumbent metric is not a frozen Pareto objective: "
                f"{policy.incumbent_metric}"
            )
        if policy.incumbent_metric not in runner.reference_metrics:
            raise ValueError(
                f"baseline reference metric is missing: {policy.incumbent_metric}"
            )
        self.runner = runner
        self.policy = policy
        self.run_dir = runner.run_dir
        self._bind_controller()

    def _bind_controller(self) -> None:
        payload = {
            "schema_version": "1.0",
            "controller": "M2EscapeCampaignController",
            "policy": self.policy.model_dump(mode="json"),
            "policy_sha256": hashlib.sha256(
                self.policy.model_dump_json().encode("utf-8")
            ).hexdigest(),
            "base_policy_sha256": hashlib.sha256(
                self.runner.policy.model_dump_json().encode("utf-8")
            ).hexdigest(),
            "stagnation_authority": "strict incumbent refresh only",
            "structural_root_definition": "candidate with genetic_parent_id=SEED",
            "parent_rights_authority": (
                "all admitted archive members; active population capacity cannot "
                "evict incumbent parent rights"
            ),
            "scientific_authority": "NONE_SCHEDULING_ONLY",
            "blind_artifacts_read": False,
        }
        path = self.run_dir / "m2_controller_manifest.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != payload:
                raise ValueError("M2 controller manifest drift")
        else:
            create_once_json(path, payload)

    def run(
        self,
        *,
        generations: int,
        generation_prefix: str = "GEN",
    ) -> AutonomousCampaignResult:
        if generations <= 0:
            raise ValueError("generations must be positive")
        self._generation_prefix = generation_prefix
        result: AutonomousCampaignResult | None = None
        for generation_index in range(1, generations + 1):
            generation_id = f"{generation_prefix}-{generation_index:03d}"
            self._plan_generation(generation_index, generation_id)
            result = self.runner.run(
                generations=generation_index,
                proposals_per_generation=1,
                max_evaluations_per_generation=1,
                generation_prefix=generation_prefix,
            )
        if result is None:  # pragma: no cover - guarded above
            raise RuntimeError("M2 controller produced no result")
        return result

    def _plan_generation(self, generation_index: int, generation_id: str) -> None:
        generation_dir = self.run_dir / "generations" / generation_id
        trace_path = generation_dir / "policy_effect_trace.json"
        state_path = generation_dir / "m2_controller_state.json"
        director_path = generation_dir / "research_director_decision.json"
        if trace_path.exists() or state_path.exists() or director_path.exists():
            if not (trace_path.exists() and state_path.exists() and director_path.exists()):
                raise ValueError(f"partial M2 generation plan: {generation_id}")
            state = M2ControllerTrace.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
            if state.policy_id != self.policy.policy_id:
                raise ValueError(f"M2 state policy drift: {generation_id}")
            return

        state = self._reconstruct_state(generation_index)
        stagnant = state["stagnant_generations"]
        escape_remaining = state["escape_budget_remaining"]
        escape_triggered = False
        if escape_remaining == 0 and stagnant >= self.policy.stagnation_generations:
            escape_remaining = self.policy.escape_budget_generations
            escape_triggered = True
        mode = (
            DiscoveryMode.BREAKTHROUGH
            if escape_remaining > 0
            else DiscoveryMode.NORMAL
        )
        mix = (
            self.policy.breakthrough_mutation_mix
            if mode is DiscoveryMode.BREAKTHROUGH
            else self.policy.mutation_operator_mix
        )
        mutation = mutation_schedule(
            mix,
            count=1,
            offset=generation_index - 1,
        )[0]
        parent_pool, preferred, admitted = self._parent_pool(
            state=state,
            mode=mode,
            mutation=mutation,
        )
        candidate_id = f"{generation_id}-C01"
        director = ResearchDirectorDecision(
            generation_id=generation_id,
            primary_action=(
                ResearchAction.BREAKTHROUGH
                if mode is DiscoveryMode.BREAKTHROUGH
                else ResearchAction.MUTATE
            ),
            executable_action=(
                ResearchAction.BREAKTHROUGH
                if mode is DiscoveryMode.BREAKTHROUGH
                else ResearchAction.MUTATE
            ),
            rationale=(
                [
                    "Strict incumbent-refresh stagnation threshold reached or "
                    "protected escape budget remains",
                    f"Protected structural escape slots before generation: {escape_remaining}",
                    "Local control/simplification/failure-local proposals are prohibited",
                ]
                if mode is DiscoveryMode.BREAKTHROUGH
                else [
                    "Strict incumbent-refresh stagnation threshold not reached",
                    "Continue the frozen normal mutation allocation",
                ]
            ),
            recommended_mutation_mix=mix,
        )
        trace = PolicyEffectTrace(
            generation_id=generation_id,
            policy_id=self.policy.policy_id,
            mode=mode,
            reasons=(
                [
                    "INCUMBENT_STAGNATION_THRESHOLD_REACHED"
                    if escape_triggered
                    else "PROTECTED_ESCAPE_BUDGET",
                    self.policy.stagnation_response,
                ]
                if mode is DiscoveryMode.BREAKTHROUGH
                else ["NORMAL_SEARCH"]
            ),
            eligible_parent_ids=parent_pool,
            mutation_assignments={candidate_id: mutation},
            moonshot_candidate_ids=[],
            parent_selector=self.policy.parent_selector,
            context_compiler=self.policy.context_compiler,
            island_assignments={candidate_id: self.policy.island_ids[0]},
            parent_pools_by_island={self.policy.island_ids[0]: parent_pool},
            parent_roles={
                candidate_id: roles
                for candidate_id, roles in state["parent_roles"].items()
                if candidate_id in parent_pool
            },
            migrations=[],
            max_parallel_proposals=1,
            max_parallel_evaluations=1,
        )
        m2_trace = M2ControllerTrace(
            generation_id=generation_id,
            policy_id=self.policy.policy_id,
            mode=mode,
            incumbent_metric=self.policy.incumbent_metric,
            incumbent_value_before=state["incumbent_value"],
            stagnant_generations_before=stagnant,
            escape_budget_remaining_before=escape_remaining,
            escape_triggered=escape_triggered,
            mutation_assignment=mutation,
            parent_pool=parent_pool,
            preferred_parent_ids=preferred,
            admitted_parent_ids=admitted,
            objective_values=state["objective_values"],
            root_lineages=state["root_lineages"],
            required_structural_transition=mode is DiscoveryMode.BREAKTHROUGH,
            required_seed_root=(
                mutation is MutationType.RESTART
                and self.policy.force_seed_restart_roots
            ),
        )
        create_once_json(director_path, director)
        create_once_json(trace_path, trace)
        create_once_json(state_path, m2_trace)

    def _reconstruct_state(self, generation_index: int) -> dict[str, object]:
        metric = self.policy.incumbent_metric
        direction = self.runner.contract.metrics.pareto_objectives[metric]
        incumbent = float(self.runner.reference_metrics[metric])
        stagnant = 0
        escape_remaining = 0
        objective_values: dict[str, float] = {}
        root_lineages: dict[str, str] = {"SEED": "SEED"}

        for index in range(1, generation_index):
            generation_id = f"{self._generation_prefix}-{index:03d}"
            candidate_id = f"{generation_id}-C01"
            state_path = (
                self.run_dir
                / "generations"
                / generation_id
                / "m2_controller_state.json"
            )
            state = M2ControllerTrace.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
            proposal_path = (
                self.run_dir
                / "generations"
                / generation_id
                / "proposals"
                / f"{candidate_id}.json"
            )
            if proposal_path.exists():
                proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
                genome = proposal["acquisition"]["candidate"]
                parent = str(genome.get("genetic_parent_id") or genome["parent_ids"][0])
                root_lineages[candidate_id] = (
                    candidate_id
                    if parent == "SEED"
                    else root_lineages.get(parent, parent)
                )

            value = self._candidate_objective(candidate_id)
            refreshed = False
            if value is not None:
                objective_values[candidate_id] = value
                delta = self.policy.incumbent_min_delta
                refreshed = (
                    value > incumbent + delta
                    if direction is ObjectiveDirection.MAXIMIZE
                    else value < incumbent - delta
                )
                if refreshed:
                    incumbent = value
            stagnant = 0 if refreshed else stagnant + 1
            if state.mode is DiscoveryMode.BREAKTHROUGH:
                escape_remaining = max(
                    state.escape_budget_remaining_before - 1,
                    0,
                )
            else:
                escape_remaining = 0

        active_snapshot = self.runner.population.snapshot().get(
            self.policy.island_ids[0], []
        )
        archive_snapshot = [
            item.model_dump(mode="json")
            for item in self.runner.population._all_members(  # noqa: SLF001
                self.policy.island_ids[0], active_only=False
            )
        ]
        parent_roles = {
            str(row["candidate_id"]): [str(role) for role in row["roles"]]
            for row in archive_snapshot
        }
        return {
            "incumbent_value": incumbent,
            "stagnant_generations": stagnant,
            "escape_budget_remaining": escape_remaining,
            "objective_values": objective_values,
            "root_lineages": root_lineages,
            "population": archive_snapshot,
            "active_population": active_snapshot,
            "parent_roles": parent_roles,
        }

    def _candidate_objective(self, candidate_id: str) -> float | None:
        receipt_dir = self.run_dir / "candidates" / candidate_id / "receipts"
        receipts = sorted(receipt_dir.glob("*.json")) if receipt_dir.exists() else []
        if not receipts:
            return None
        envelope = json.loads(receipts[0].read_text(encoding="utf-8"))
        receipt = envelope["receipt"]
        evaluation = receipt["evaluation_input"]
        if (
            evaluation["mechanics_status"] != "PASS"
            or not evaluation["data_eligible"]
            or evaluation["protocol_violations"]
            or not all(bool(value) for value in evaluation["controls"].values())
        ):
            return None
        value = evaluation["metrics"].get(self.policy.incumbent_metric)
        return None if value is None else float(value)

    def _parent_pool(
        self,
        *,
        state: dict[str, object],
        mode: DiscoveryMode,
        mutation: MutationType,
    ) -> tuple[list[str], list[str], list[str]]:
        population = list(state["population"])
        admitted = [
            str(row["candidate_id"])
            for row in list(state["active_population"])
        ]
        if (
            mode is DiscoveryMode.BREAKTHROUGH
            and mutation is MutationType.RESTART
            and self.policy.force_seed_restart_roots
        ):
            return ["SEED"], [], admitted

        objective_values = dict(state["objective_values"])
        incumbent = float(state["incumbent_value"])
        direction = self.runner.contract.metrics.pareto_objectives[
            self.policy.incumbent_metric
        ]
        tolerance = max(abs(incumbent), 1e-12) * (
            self.policy.parent_quality_guardrail_fraction
        )
        eligible = []
        for row in population:
            candidate_id = str(row["candidate_id"])
            value = objective_values.get(candidate_id)
            if value is None:
                continue
            within = (
                value >= incumbent - tolerance
                if direction is ObjectiveDirection.MAXIMIZE
                else value <= incumbent + tolerance
            )
            if within:
                eligible.append(row)
        sign = 1.0 if direction is ObjectiveDirection.MAXIMIZE else -1.0
        eligible.sort(
            key=lambda row: (
                sign * objective_values[str(row["candidate_id"])],
                float(row["novelty"]),
                float(row["information_gain"]),
                str(row["admitted_generation"]),
                str(row["candidate_id"]),
            ),
            reverse=True,
        )
        roots = dict(state["root_lineages"])
        selected = eligible[: self.policy.parents_per_island]
        if (
            mode is DiscoveryMode.BREAKTHROUGH
            and self.policy.prefer_distinct_root_context
            and selected
        ):
            champion = selected[0]
            champion_id = str(champion["candidate_id"])
            champion_root = roots.get(champion_id, champion_id)
            different = next(
                (
                    row
                    for row in eligible[1:]
                    if roots.get(str(row["candidate_id"]), str(row["candidate_id"]))
                    != champion_root
                ),
                None,
            )
            if different is not None:
                selected = [champion, different]
        preferred = [str(row["candidate_id"]) for row in selected]
        return preferred or ["SEED"], preferred, admitted
