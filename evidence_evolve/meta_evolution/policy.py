from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from evidence_evolve.governance.closure_registry import (
    ClosureRegistry,
    audit_candidate_closures,
)
from evidence_evolve.models import (
    CandidateGenome,
    MutationType,
    SearchDisposition,
    StrictModel,
)


class DiscoveryMode(StrEnum):
    NORMAL = "NORMAL"
    BREAKTHROUGH = "BREAKTHROUGH"


class PolicyEffectTrace(StrictModel):
    generation_id: str
    policy_id: str
    mode: DiscoveryMode
    reasons: list[str] = Field(default_factory=list)
    eligible_parent_ids: list[str]
    mutation_assignments: dict[str, MutationType]
    moonshot_candidate_ids: list[str] = Field(default_factory=list)
    parent_selector: str
    context_compiler: str
    island_assignments: dict[str, str] = Field(default_factory=dict)
    parent_pools_by_island: dict[str, list[str]] = Field(default_factory=dict)
    parent_roles: dict[str, list[str]] = Field(default_factory=dict)
    migrations: list[dict[str, str]] = Field(default_factory=list)
    max_parallel_proposals: int = Field(default=1, ge=1)
    max_parallel_evaluations: int = Field(default=1, ge=1)


class AcquisitionWeights(StrictModel):
    admit_probability: float = Field(default=1.0, ge=0.0)
    expected_improvement: float = Field(default=1.0, ge=0.0)
    information_gain: float = Field(default=1.0, ge=0.0)
    novelty: float = Field(default=0.5, ge=0.0)
    transfer_value: float = Field(default=0.25, ge=0.0)
    cost: float = Field(default=0.5, ge=0.0)
    redundancy: float = Field(default=0.5, ge=0.0)


class ResearchPolicyGenome(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str
    parent_selector: Literal["island_stratified"] = "island_stratified"
    context_compiler: Literal[
        "lineage_plus_failure_signature"
    ] = "lineage_plus_failure_signature"
    stagnation_response: Literal[
        "cross_family_structural_restart"
    ] = "cross_family_structural_restart"
    stagnation_generations: int = Field(default=3, ge=1)
    moonshot_fraction: float = Field(default=0.1, ge=0.0, le=0.5)
    island_ids: list[str] = Field(default_factory=lambda: ["main"], min_length=1)
    island_capacity: int = Field(default=32, ge=1)
    parents_per_island: int = Field(default=4, ge=1)
    migration_interval: int = Field(default=3, ge=1)
    migration_count: int = Field(default=1, ge=0)
    stepping_stone_min_information_gain: float = Field(
        default=0.6, ge=0.0, le=1.0
    )
    max_parallel_proposals: int = Field(default=1, ge=1, le=32)
    max_parallel_evaluations: int = Field(default=1, ge=1, le=32)
    literature_papers_per_action: int = Field(default=5, ge=0, le=25)
    repositories_per_action: int = Field(default=2, ge=0, le=10)
    source_files_per_repository: int = Field(default=3, ge=0, le=20)
    code_parent_dispositions: list[SearchDisposition] = Field(
        default_factory=lambda: [
            SearchDisposition.CODE_PARENT,
            SearchDisposition.FAILURE_DIRECTED_SEED,
        ]
    )
    mutation_operator_mix: dict[MutationType, float] = Field(
        default_factory=lambda: {
            MutationType.MECHANISM: 0.4,
            MutationType.REPRESENTATION: 0.3,
            MutationType.FAILURE_DIRECTED: 0.3,
        }
    )
    breakthrough_mutation_mix: dict[MutationType, float] = Field(
        default_factory=lambda: {
            MutationType.REPRESENTATION: 0.4,
            MutationType.CROSS_FAMILY: 0.35,
            MutationType.RESTART: 0.25,
        }
    )
    weights: AcquisitionWeights = Field(default_factory=AcquisitionWeights)

    @field_validator("island_ids")
    @classmethod
    def island_ids_are_safe_and_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("island_ids must be unique")
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
                raise ValueError(f"unsafe island id: {value}")
        return values

    @model_validator(mode="after")
    def mutation_mix_is_a_distribution(self) -> "ResearchPolicyGenome":
        for name, mix in (
            ("mutation_operator_mix", self.mutation_operator_mix),
            ("breakthrough_mutation_mix", self.breakthrough_mutation_mix),
        ):
            if not mix:
                raise ValueError(f"{name} cannot be empty")
            if any(weight < 0 for weight in mix.values()):
                raise ValueError(f"{name} weights cannot be negative")
            total = sum(mix.values())
            if abs(total - 1.0) > 1e-9:
                raise ValueError(f"{name} weights must sum to 1.0")
        if not self.code_parent_dispositions:
            raise ValueError("code_parent_dispositions cannot be empty")
        if SearchDisposition.QUARANTINE in self.code_parent_dispositions:
            raise ValueError("quarantined candidates cannot receive code parent rights")
        if self.migration_count > self.island_capacity:
            raise ValueError("migration_count cannot exceed island_capacity")
        return self


class AcquisitionSignals(StrictModel):
    admit_probability: float = Field(ge=0.0, le=1.0)
    expected_improvement: float
    information_gain: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    transfer_value: float = Field(default=0.0, ge=0.0, le=1.0)
    estimated_cost: float = Field(default=0.0, ge=0.0)
    redundancy: float = Field(default=0.0, ge=0.0, le=1.0)


class CandidateAcquisition(StrictModel):
    candidate: CandidateGenome
    signals: AcquisitionSignals
    verified_reopen_conditions: set[str] = Field(default_factory=set)


class AcquisitionDecision(StrictModel):
    candidate_id: str
    eligible: bool
    acquisition_score: float | None = None
    reasons: list[str] = Field(default_factory=list)


def mutation_schedule(
    mix: dict[MutationType, float],
    *,
    count: int,
    offset: int = 0,
) -> list[MutationType]:
    """Deterministically turn policy weights into behavior for proposal slots."""
    if count < 0:
        raise ValueError("count must be non-negative")
    if not mix:
        raise ValueError("mutation mix cannot be empty")
    ordered = sorted(mix, key=lambda item: item.value)
    assigned = {item: 0 for item in ordered}
    result: list[MutationType] = []
    for index in range(count + offset):
        chosen = max(
            ordered,
            key=lambda item: (
                mix[item] * (index + 1) - assigned[item],
                mix[item],
                item.value,
            ),
        )
        assigned[chosen] += 1
        if index >= offset:
            result.append(chosen)
    return result


def _score(policy: ResearchPolicyGenome, item: CandidateAcquisition) -> float:
    weights = policy.weights
    signals = item.signals
    return (
        weights.admit_probability * signals.admit_probability
        + weights.expected_improvement * signals.expected_improvement
        + weights.information_gain * signals.information_gain
        + weights.novelty * signals.novelty
        + weights.transfer_value * signals.transfer_value
        - weights.cost * signals.estimated_cost
        - weights.redundancy * signals.redundancy
    )


def rank_candidates(
    *,
    policy: ResearchPolicyGenome,
    candidates: list[CandidateAcquisition],
    closure_registry: ClosureRegistry,
) -> list[AcquisitionDecision]:
    """Rank research value while enforcing closure as a non-bypassable boundary."""
    seen: set[str] = set()
    decisions: list[AcquisitionDecision] = []
    for item in candidates:
        candidate_id = item.candidate.candidate_id
        if candidate_id in seen:
            raise ValueError(f"duplicate candidate id in acquisition pool: {candidate_id}")
        seen.add(candidate_id)
        closure = audit_candidate_closures(
            item.candidate,
            closure_registry,
            item.verified_reopen_conditions,
        )
        if not closure.allowed:
            decisions.append(
                AcquisitionDecision(
                    candidate_id=candidate_id,
                    eligible=False,
                    reasons=closure.violations,
                )
            )
            continue
        decisions.append(
            AcquisitionDecision(
                candidate_id=candidate_id,
                eligible=True,
                acquisition_score=_score(policy, item),
                reasons=["ELIGIBLE_FOR_SCHEDULING_ONLY"],
            )
        )
    return sorted(
        decisions,
        key=lambda decision: (
            not decision.eligible,
            -(decision.acquisition_score or 0.0),
            decision.candidate_id,
        ),
    )
