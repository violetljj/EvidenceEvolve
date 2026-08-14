from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from evidence_evolve.models import (
    CandidateGenome,
    EvaluationInput,
    GateVerdict,
    ObjectiveDirection,
    ResearchContract,
    ScientificOutcome,
    StrictModel,
)


class SignatureExpectation(StrEnum):
    IMPROVE = "IMPROVE"
    UNCHANGED = "UNCHANGED"


class SignatureJudgement(StrEnum):
    MATCHED = "MATCHED"
    CONTRADICTED = "CONTRADICTED"
    NOT_ASSESSED = "NOT_ASSESSED"


class MechanismSupport(StrEnum):
    INTERVENTION_SUPPORTED = "INTERVENTION_SUPPORTED"
    PREDICTION_SUPPORTED = "PREDICTION_SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class MetricSignatureCheck(StrictModel):
    metric: str
    expectation: SignatureExpectation
    direction: ObjectiveDirection | None = None
    reference_value: float | None = None
    observed_value: float | None = None
    observed_delta: float | None = None
    tolerance: float
    judgement: SignatureJudgement
    reason: str


class MechanismAssessment(StrictModel):
    candidate_id: str
    support: MechanismSupport
    authority: Literal["SCHEDULING_ONLY"] = "SCHEDULING_ONLY"
    signature_checks: dict[str, MetricSignatureCheck] = Field(default_factory=dict)
    controls_complete: bool
    ablations_complete: bool
    supporting_ablations: list[str] = Field(default_factory=list)
    missing_ablations: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def _check_metric(
    *,
    metric: str,
    expectation: SignatureExpectation,
    direction: ObjectiveDirection | None,
    reference_value: float | None,
    observed_value: float | None,
    tolerance: float,
) -> MetricSignatureCheck:
    if direction is None:
        return MetricSignatureCheck(
            metric=metric,
            expectation=expectation,
            direction=None,
            reference_value=reference_value,
            observed_value=observed_value,
            tolerance=tolerance,
            judgement=SignatureJudgement.NOT_ASSESSED,
            reason="METRIC_DIRECTION_NOT_FROZEN",
        )
    if reference_value is None:
        return MetricSignatureCheck(
            metric=metric,
            expectation=expectation,
            direction=direction,
            reference_value=None,
            observed_value=observed_value,
            tolerance=tolerance,
            judgement=SignatureJudgement.NOT_ASSESSED,
            reason="REFERENCE_METRIC_MISSING",
        )
    if observed_value is None:
        return MetricSignatureCheck(
            metric=metric,
            expectation=expectation,
            direction=direction,
            reference_value=reference_value,
            observed_value=None,
            tolerance=tolerance,
            judgement=SignatureJudgement.NOT_ASSESSED,
            reason="OBSERVED_METRIC_MISSING",
        )

    delta = observed_value - reference_value
    if expectation is SignatureExpectation.UNCHANGED:
        matched = abs(delta) <= tolerance
    elif direction is ObjectiveDirection.MINIMIZE:
        matched = delta < -tolerance
    else:
        matched = delta > tolerance
    return MetricSignatureCheck(
        metric=metric,
        expectation=expectation,
        direction=direction,
        reference_value=reference_value,
        observed_value=observed_value,
        observed_delta=delta,
        tolerance=tolerance,
        judgement=(
            SignatureJudgement.MATCHED
            if matched
            else SignatureJudgement.CONTRADICTED
        ),
        reason="EXPECTED_SIGNATURE_MATCHED" if matched else "EXPECTED_SIGNATURE_MISSED",
    )


def assess_mechanism(
    *,
    contract: ResearchContract,
    candidate: CandidateGenome,
    evaluation: EvaluationInput,
    verdict: GateVerdict,
    reference_metrics: dict[str, float],
    ablation_results: dict[str, bool] | None = None,
    tolerance: float = 0.0,
) -> MechanismAssessment:
    """Test preregistered predictions without creating scientific authority.

    ``True`` in ``ablation_results`` means the frozen evaluator observed the
    preregistered effect of that ablation. LLM prose is never accepted here.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    if evaluation.candidate.candidate_id != candidate.candidate_id:
        raise ValueError("evaluation candidate does not match mechanism candidate")

    directions = contract.metrics.pareto_objectives
    checks: dict[str, MetricSignatureCheck] = {}
    for expectation, metrics in (
        (SignatureExpectation.IMPROVE, candidate.expected_signature.improve),
        (SignatureExpectation.UNCHANGED, candidate.expected_signature.unchanged),
    ):
        for metric in metrics:
            checks[metric] = _check_metric(
                metric=metric,
                expectation=expectation,
                direction=directions.get(metric),
                reference_value=reference_metrics.get(metric),
                observed_value=evaluation.metrics.get(metric),
                tolerance=tolerance,
            )

    ablations = ablation_results or {}
    supporting_ablations = sorted(
        name for name in candidate.ablation_plan if ablations.get(name) is True
    )
    missing_ablations = sorted(
        name for name in candidate.ablation_plan if ablations.get(name) is not True
    )
    ablations_complete = bool(candidate.ablation_plan) and not missing_ablations
    controls_complete = all(
        evaluation.controls.get(name) is True for name in candidate.required_controls
    )

    reasons: list[str] = []
    invalid_evidence = (
        not verdict.protocol_valid
        or verdict.scientific_outcome
        in {
            ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
            ScientificOutcome.NOT_EVALUABLE_DATA,
        }
    )
    judgements = {check.judgement for check in checks.values()}
    if invalid_evidence:
        support = MechanismSupport.NOT_EVALUABLE
        reasons.append("SCIENTIFIC_EVIDENCE_NOT_ELIGIBLE_FOR_MECHANISM_ASSESSMENT")
    elif SignatureJudgement.CONTRADICTED in judgements:
        support = MechanismSupport.CONTRADICTED
        reasons.append("PREREGISTERED_SIGNATURE_CONTRADICTED")
    elif not checks or SignatureJudgement.NOT_ASSESSED in judgements:
        support = MechanismSupport.INCONCLUSIVE
        reasons.append("PREREGISTERED_SIGNATURE_INCOMPLETE")
    elif not controls_complete:
        support = MechanismSupport.INCONCLUSIVE
        reasons.append("REQUIRED_CONTROLS_INCOMPLETE")
    elif ablations_complete:
        support = MechanismSupport.INTERVENTION_SUPPORTED
        reasons.append("SIGNATURE_CONTROLS_AND_ABLATIONS_MATCHED")
    else:
        support = MechanismSupport.PREDICTION_SUPPORTED
        reasons.append("SIGNATURE_AND_CONTROLS_MATCHED_WITHOUT_COMPLETE_ABLATION")

    if missing_ablations:
        reasons.append(f"ABLATIONS_INCOMPLETE:{','.join(missing_ablations)}")
    return MechanismAssessment(
        candidate_id=candidate.candidate_id,
        support=support,
        signature_checks=checks,
        controls_complete=controls_complete,
        ablations_complete=ablations_complete,
        supporting_ablations=supporting_ablations,
        missing_ablations=missing_ablations,
        reasons=reasons,
    )
