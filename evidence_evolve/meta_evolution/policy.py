from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from evidence_evolve.governance.closure_registry import (
    ClosureRegistry,
    audit_candidate_closures,
)
from evidence_evolve.models import CandidateGenome, MutationType, StrictModel


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
    parent_selector: str = "pareto_complementary"
    inspiration_selector: str = "mechanism_diverse"
    abstraction_selector: str = "stagnation_adaptive"
    context_compiler: str = "lineage_plus_failure_signature"
    evaluation_cascade: list[str] = Field(default_factory=lambda: ["M0_MECHANICS"])
    ablation_trigger: str = "signature_supported"
    stagnation_response: str = "cross_family_structural_restart"
    mutation_operator_mix: dict[MutationType, float] = Field(
        default_factory=lambda: {
            MutationType.MECHANISM: 0.4,
            MutationType.REPRESENTATION: 0.3,
            MutationType.FAILURE_DIRECTED: 0.3,
        }
    )
    weights: AcquisitionWeights = Field(default_factory=AcquisitionWeights)

    @model_validator(mode="after")
    def mutation_mix_is_a_distribution(self) -> "ResearchPolicyGenome":
        if not self.mutation_operator_mix:
            raise ValueError("mutation_operator_mix cannot be empty")
        if any(weight < 0 for weight in self.mutation_operator_mix.values()):
            raise ValueError("mutation operator weights cannot be negative")
        total = sum(self.mutation_operator_mix.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError("mutation operator weights must sum to 1.0")
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
