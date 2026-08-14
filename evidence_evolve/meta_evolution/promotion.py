from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from evidence_evolve.models import StrictModel


class PolicyBenchmarkResult(StrictModel):
    policy_id: str
    suite_id: str
    task_count: int = Field(ge=1)
    held_out: bool
    blind: bool
    blind_breakthrough_rate: float = Field(ge=0.0, le=1.0)
    valid_improvement_per_cost: float = Field(ge=0.0)
    hypothesis_calibration: float = Field(ge=0.0, le=1.0)
    mechanism_prediction_accuracy: float = Field(ge=0.0, le=1.0)
    redundant_experiment_rate: float = Field(ge=0.0, le=1.0)
    closure_violations: int = Field(ge=0)
    fresh_set_robustness: float = Field(ge=0.0, le=1.0)
    reproducibility_rate: float = Field(ge=0.0, le=1.0)


class PolicyPromotionProtocol(StrictModel):
    min_tasks: int = Field(default=3, ge=1)
    min_breakthrough_delta: float = 0.0
    min_efficiency_delta: float = 0.0
    max_calibration_regression: float = Field(default=0.0, ge=0.0)
    max_mechanism_accuracy_regression: float = Field(default=0.0, ge=0.0)
    max_redundancy_regression: float = Field(default=0.0, ge=0.0)
    min_fresh_set_robustness: float = Field(default=0.0, ge=0.0, le=1.0)
    min_reproducibility_rate: float = Field(default=1.0, ge=0.0, le=1.0)


class PolicyPromotionDecision(StrEnum):
    ELIGIBLE_FOR_HUMAN_PROMOTION = "ELIGIBLE_FOR_HUMAN_PROMOTION"
    HOLD = "HOLD"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class PolicyPromotionVerdict(StrictModel):
    candidate_policy_id: str
    baseline_policy_id: str
    suite_id: str
    decision: PolicyPromotionDecision
    authority: Literal["META_EVALUATION_ONLY"] = "META_EVALUATION_ONLY"
    final_promotion_requires_human: Literal[True] = True
    deltas: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


def evaluate_policy_promotion(
    *,
    candidate: PolicyBenchmarkResult,
    baseline: PolicyBenchmarkResult,
    protocol: PolicyPromotionProtocol,
) -> PolicyPromotionVerdict:
    """Compare research policies on blind held-out evidence, never task prose."""
    if candidate.suite_id != baseline.suite_id:
        raise ValueError("candidate and baseline must use the same meta benchmark suite")
    deltas = {
        "blind_breakthrough_rate": (
            candidate.blind_breakthrough_rate - baseline.blind_breakthrough_rate
        ),
        "valid_improvement_per_cost": (
            candidate.valid_improvement_per_cost
            - baseline.valid_improvement_per_cost
        ),
        "hypothesis_calibration": (
            candidate.hypothesis_calibration - baseline.hypothesis_calibration
        ),
        "mechanism_prediction_accuracy": (
            candidate.mechanism_prediction_accuracy
            - baseline.mechanism_prediction_accuracy
        ),
        "redundant_experiment_rate": (
            candidate.redundant_experiment_rate - baseline.redundant_experiment_rate
        ),
        "fresh_set_robustness": (
            candidate.fresh_set_robustness - baseline.fresh_set_robustness
        ),
        "reproducibility_rate": (
            candidate.reproducibility_rate - baseline.reproducibility_rate
        ),
    }
    if (
        not candidate.held_out
        or not candidate.blind
        or not baseline.held_out
        or not baseline.blind
        or candidate.task_count < protocol.min_tasks
        or baseline.task_count < protocol.min_tasks
    ):
        return PolicyPromotionVerdict(
            candidate_policy_id=candidate.policy_id,
            baseline_policy_id=baseline.policy_id,
            suite_id=candidate.suite_id,
            decision=PolicyPromotionDecision.NOT_EVALUABLE,
            deltas=deltas,
            reasons=["BLIND_HELD_OUT_META_EVIDENCE_INSUFFICIENT"],
        )

    reasons: list[str] = []
    if candidate.closure_violations:
        reasons.append("CLOSURE_VIOLATIONS_PRESENT")
    if deltas["hypothesis_calibration"] < -protocol.max_calibration_regression:
        reasons.append("HYPOTHESIS_CALIBRATION_REGRESSED")
    if (
        deltas["mechanism_prediction_accuracy"]
        < -protocol.max_mechanism_accuracy_regression
    ):
        reasons.append("MECHANISM_PREDICTION_REGRESSED")
    if deltas["redundant_experiment_rate"] > protocol.max_redundancy_regression:
        reasons.append("REDUNDANT_EXPERIMENT_RATE_REGRESSED")
    if candidate.fresh_set_robustness < protocol.min_fresh_set_robustness:
        reasons.append("FRESH_SET_ROBUSTNESS_BELOW_FLOOR")
    if candidate.reproducibility_rate < protocol.min_reproducibility_rate:
        reasons.append("REPRODUCIBILITY_BELOW_FLOOR")

    discovery_improved = (
        deltas["blind_breakthrough_rate"] > protocol.min_breakthrough_delta
        or deltas["valid_improvement_per_cost"] > protocol.min_efficiency_delta
    )
    if not discovery_improved:
        reasons.append("NO_STRICT_DISCOVERY_PROGRESS")
    decision = (
        PolicyPromotionDecision.HOLD
        if reasons
        else PolicyPromotionDecision.ELIGIBLE_FOR_HUMAN_PROMOTION
    )
    return PolicyPromotionVerdict(
        candidate_policy_id=candidate.policy_id,
        baseline_policy_id=baseline.policy_id,
        suite_id=candidate.suite_id,
        decision=decision,
        deltas=deltas,
        reasons=reasons or ["BLIND_HELD_OUT_META_GATES_PASS"],
    )
