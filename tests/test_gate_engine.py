from evidence_evolve.governance.gate_engine import GateEngine
from evidence_evolve.models import (
    EvaluationInput,
    GateDecision,
    MechanicsStatus,
    ResearchStage,
    ScientificOutcome,
)


def evaluation(contract, candidate, **updates) -> EvaluationInput:
    values = {
        "contract_sha256": contract.lock.content_sha256,
        "candidate": candidate,
        "stage": ResearchStage.H0_REAL_HEADROOM,
        "mechanics_status": MechanicsStatus.PASS,
        "data_eligible": True,
        "metrics": {"false_block_delta_pp": 0.0, "clearance_mae_delta": -0.1},
        "controls": {"wrong_factor": True, "zero_factor": True},
        "scientific_outcome": ScientificOutcome.POSITIVE_HEADROOM,
    }
    values.update(updates)
    return EvaluationInput(**values)


def test_protocol_tampering_is_invalid(contract, candidate) -> None:
    verdict = GateEngine(contract).evaluate(
        evaluation(contract, candidate, protocol_violations=["DENIED_PATH:evaluator.py"])
    )
    assert verdict.decision is GateDecision.INVALID_PROTOCOL_TAMPERING
    assert verdict.scientific_outcome is ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER


def test_missing_truth_is_not_a_negative(contract, candidate) -> None:
    verdict = GateEngine(contract).evaluate(
        evaluation(
            contract,
            candidate,
            data_eligible=False,
            data_ineligibility_reasons=["FRESH_PAIRED_TRUTH_MISSING"],
            scientific_outcome=ScientificOutcome.NOT_EVALUABLE_DATA,
        )
    )
    assert verdict.decision is GateDecision.PAUSE_NOT_EVALUABLE
    assert verdict.scientific_outcome is ScientificOutcome.NOT_EVALUABLE_DATA


def test_false_block_gate_cannot_be_averaged_away(contract, candidate) -> None:
    verdict = GateEngine(contract).evaluate(
        evaluation(
            contract,
            candidate,
            metrics={"false_block_delta_pp": 0.01, "clearance_mae_delta": -999.0},
        )
    )
    assert verdict.decision is GateDecision.KILL
    assert verdict.scientific_outcome is ScientificOutcome.VALID_NEGATIVE


def test_missing_hard_metric_is_not_evaluable(contract, candidate) -> None:
    verdict = GateEngine(contract).evaluate(
        evaluation(contract, candidate, metrics={"clearance_mae_delta": -0.1})
    )
    assert verdict.decision is GateDecision.PAUSE_NOT_EVALUABLE


def test_missing_control_is_invalid_mechanics(contract, candidate) -> None:
    verdict = GateEngine(contract).evaluate(
        evaluation(contract, candidate, controls={"wrong_factor": True})
    )
    assert verdict.decision is GateDecision.REPAIR_IMPLEMENTATION
    assert verdict.scientific_outcome is ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER


def test_gate_uses_frozen_controls_not_candidate_claims(contract, candidate) -> None:
    candidate.required_controls = ["wrong_factor"]
    verdict = GateEngine(contract).evaluate(
        evaluation(contract, candidate, controls={"wrong_factor": True})
    )
    assert verdict.decision is GateDecision.REPAIR_IMPLEMENTATION
    assert verdict.reasons == ["REQUIRED_CONTROLS_INCOMPLETE:zero_factor"]


def test_mechanics_not_run_cannot_advance(contract, candidate) -> None:
    verdict = GateEngine(contract).evaluate(
        evaluation(contract, candidate, mechanics_status=MechanicsStatus.NOT_RUN)
    )
    assert verdict.decision is GateDecision.REPAIR_IMPLEMENTATION


def test_positive_outcome_requires_all_hard_gates(contract, candidate) -> None:
    verdict = GateEngine(contract).evaluate(evaluation(contract, candidate))
    assert verdict.decision is GateDecision.ADMIT
    assert verdict.scientific_outcome is ScientificOutcome.POSITIVE_HEADROOM
