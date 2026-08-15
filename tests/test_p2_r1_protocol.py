from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_evolve.models import ScientificOutcome
from evidence_evolve.proposals.non_inferiority import (
    P2R1Protocol,
    load_and_validate_p2_r1_protocol,
)
from evidence_evolve.proposals.parity_analysis import (
    ArmRun,
    P2R1AnalysisInput,
    ProposalSlot,
    analyze_p2_r1,
)


REPO = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPO / "research/parity/shinka_native_p2_r1.protocol.json"


def _protocol() -> P2R1Protocol:
    return load_and_validate_p2_r1_protocol(PROTOCOL_PATH, repo=REPO)


def _slot(slot: int, *, valid: bool = True, started: bool = True) -> ProposalSlot:
    if not started:
        return ProposalSlot(
            slot=slot,
            model_invocation_started=False,
            proposal_received=False,
            proposal_extracted=False,
            materialized=False,
            compiled=False,
            evaluator_reached=False,
            evaluator_valid=False,
        )
    return ProposalSlot(
        slot=slot,
        model_invocation_started=True,
        proposal_received=True,
        proposal_extracted=True,
        materialized=True,
        compiled=True,
        evaluator_reached=True,
        evaluator_valid=valid,
        score=1.01 if valid else None,
    )


def _input(
    protocol: P2R1Protocol,
    *,
    native_invalid_slots: set[tuple[int, int]] | None = None,
    missing_started_slot: tuple[int, str, int] | None = None,
) -> P2R1AnalysisInput:
    invalid = native_invalid_slots or set()
    runs = []
    for block in range(1, 11):
        for arm in ("official", "native"):
            slots = []
            for slot in range(1, 6):
                started = missing_started_slot != (block, arm, slot)
                valid = not (arm == "native" and (block, slot) in invalid)
                slots.append(_slot(slot, valid=valid, started=started))
            runs.append(
                ArmRun(
                    block=block,
                    arm=arm,
                    baseline_score=1.0,
                    slots=slots,
                    observed_input_tokens=100,
                    observed_output_tokens=20,
                    observed_cost=None,
                    wall_seconds=10.0,
                    resume_consistent=True,
                )
            )
    return P2R1AnalysisInput(
        protocol_id=protocol.protocol_id,
        protocol_sha256=protocol.protocol_sha256,
        runs=runs,
    )


def test_repository_p2_r1_protocol_is_frozen_and_hash_valid() -> None:
    protocol = _protocol()

    assert protocol.protocol_status == "FROZEN_NOT_STARTED"
    assert protocol.execution_started is False
    assert protocol.remote_model_calls_at_freeze == 0
    assert protocol.provider.model == "gpt-5.6-terra"
    assert protocol.design.arm_budgets["official"] == (
        protocol.design.arm_budgets["native"]
    )
    assert protocol.invalid_handling.no_valid_only_resampling is True


def test_protocol_rejects_post_hoc_replacement_or_valid_only_analysis() -> None:
    payload = json.loads(PROTOCOL_PATH.read_text())
    payload["design"]["no_post_hoc_replacement"] = False
    payload["invalid_handling"]["no_valid_only_resampling"] = False

    with pytest.raises(ValidationError):
        P2R1Protocol.model_validate(payload)


def test_primary_score_cannot_rescue_invalid_and_useful_rate_guardrails() -> None:
    protocol = _protocol()
    invalid = {(block, slot) for block in range(1, 11) for slot in (1, 2)}

    result = analyze_p2_r1(
        _input(protocol, native_invalid_slots=invalid),
        protocol,
    )

    assert result.primary_gate_passed is True
    assert result.invalid_rate_guardrail_passed is False
    assert result.useful_rate_guardrail_passed is False
    assert result.non_inferiority_assessment == "NOT_SUPPORTED"
    assert result.scientific_outcome is ScientificOutcome.VALID_NEGATIVE
    assert result.per_arm_funnel["native"].evaluator_valid == 30
    assert result.per_arm_funnel["native"].scheduled == 50


def test_missing_model_call_is_not_reclassified_as_a_negative() -> None:
    protocol = _protocol()

    result = analyze_p2_r1(
        _input(protocol, missing_started_slot=(1, "native", 1)),
        protocol,
    )

    assert result.statistical_eligibility == "NOT_EVALUABLE_DATA"
    assert result.non_inferiority_assessment == "INCONCLUSIVE"
    assert result.scientific_outcome is ScientificOutcome.NOT_EVALUABLE_DATA
    assert "UNEQUAL_OR_MISSING_MODEL_CALLS:native" in result.ineligibility_reasons
