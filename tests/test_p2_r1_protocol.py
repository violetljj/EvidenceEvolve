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
    _gte_with_tolerance,
    _lte_with_tolerance,
    analyze_p2_r1,
)


REPO = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPO / "research/parity/shinka_native_p2_r1.protocol.json"


def _protocol() -> P2R1Protocol:
    return load_and_validate_p2_r1_protocol(PROTOCOL_PATH, repo=REPO)


def _slot(
    slot: int,
    *,
    valid: bool = True,
    started: bool = True,
    score: float = 1.01,
) -> ProposalSlot:
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
    payload_sha256 = "c" * 64
    return ProposalSlot(
        slot=slot,
        model_invocation_started=True,
        proposal_received=True,
        proposal_extracted=True,
        materialized=True,
        compiled=True,
        evaluator_reached=True,
        evaluator_valid=valid,
        score=score if valid else None,
        rendered_system_prompt_sha256="a" * 64,
        rendered_user_prompt_sha256="b" * 64,
        request_payload_sha256=payload_sha256,
        transport_attempt_payload_sha256s=[payload_sha256],
    )


def _terminal_slot(slot: int, terminal: str) -> ProposalSlot:
    stages = {
        "MODEL_INVOCATION_NOT_STARTED": (False, False, False, False, False, False),
        "MODEL_RESPONSE_MISSING": (True, False, False, False, False, False),
        "PROPOSAL_EXTRACTION_FAILED": (True, True, False, False, False, False),
        "MATERIALIZATION_FAILED": (True, True, True, False, False, False),
        "COMPILE_FAILED": (True, True, True, True, False, False),
        "EVALUATOR_NOT_REACHED": (True, True, True, True, True, False),
        "EVALUATOR_INVALID": (True, True, True, True, True, True),
    }
    values = stages[terminal]
    started = values[0]
    payload_sha256 = "c" * 64 if started else None
    return ProposalSlot(
        slot=slot,
        model_invocation_started=values[0],
        proposal_received=values[1],
        proposal_extracted=values[2],
        materialized=values[3],
        compiled=values[4],
        evaluator_reached=values[5],
        evaluator_valid=False,
        rendered_system_prompt_sha256="a" * 64 if started else None,
        rendered_user_prompt_sha256="b" * 64 if started else None,
        request_payload_sha256=payload_sha256,
        transport_attempt_payload_sha256s=[payload_sha256] if started else [],
    )


def _input(
    protocol: P2R1Protocol,
    *,
    native_invalid_slots: set[tuple[int, int]] | None = None,
    missing_started_slot: tuple[int, str, int] | None = None,
    slot_overrides: dict[tuple[int, str, int], ProposalSlot] | None = None,
    baseline_overrides: dict[tuple[int, str], float] | None = None,
) -> P2R1AnalysisInput:
    invalid = native_invalid_slots or set()
    overrides = slot_overrides or {}
    baselines = baseline_overrides or {}
    runs = []
    for block in range(1, 11):
        for arm in ("official", "native"):
            slots = []
            for slot in range(1, 6):
                if (block, arm, slot) in overrides:
                    slots.append(overrides[(block, arm, slot)])
                    continue
                started = missing_started_slot != (block, arm, slot)
                valid = not (arm == "native" and (block, slot) in invalid)
                slots.append(_slot(slot, valid=valid, started=started))
            runs.append(
                ArmRun(
                    block=block,
                    arm=arm,
                    baseline_score=baselines.get((block, arm), 1.0),
                    initial_program_sha256=protocol.frozen_assets[
                        "initial_program"
                    ].sha256,
                    evaluator_sha256=protocol.frozen_assets["evaluator"].sha256,
                    config_sha256=protocol.frozen_assets["config"].sha256,
                    initial_incumbent_sha256=protocol.frozen_assets[
                        "initial_program"
                    ].sha256,
                    state_namespace=f"p2-r1-block-{block:02d}-{arm}",
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

    assert (
        protocol.protocol_status
        == "P2_R1_EXECUTION_COMPLETE_FROZEN_COMMITTED_NOT_STARTED"
    )
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


def test_guardrails_admit_exact_10pp_boundary_and_reject_the_next_slot() -> None:
    protocol = _protocol()
    exact_boundary = {(1, slot) for slot in range(1, 6)}
    beyond_boundary = {*exact_boundary, (2, 1)}

    exact = analyze_p2_r1(
        _input(protocol, native_invalid_slots=exact_boundary), protocol
    )
    beyond = analyze_p2_r1(
        _input(protocol, native_invalid_slots=beyond_boundary), protocol
    )

    assert exact.evaluator_invalid_rate_delta_native_minus_official == pytest.approx(
        0.10
    )
    assert exact.intent_to_evaluate_useful_rate_delta_native_minus_official == (
        pytest.approx(-0.10)
    )
    assert exact.invalid_rate_guardrail_passed is True
    assert exact.useful_rate_guardrail_passed is True
    assert beyond.invalid_rate_guardrail_passed is False
    assert beyond.useful_rate_guardrail_passed is False


def test_guardrail_float_tolerance_is_explicit_and_deterministic() -> None:
    tolerance = 1e-12

    assert _lte_with_tolerance(0.10, 0.10, tolerance)
    assert _lte_with_tolerance(0.1000000000001, 0.10, tolerance)
    assert not _lte_with_tolerance(0.1000000001, 0.10, tolerance)
    assert _gte_with_tolerance(-0.10, -0.10, tolerance)
    assert _gte_with_tolerance(-0.1000000000001, -0.10, tolerance)
    assert not _gte_with_tolerance(-0.1000000001, -0.10, tolerance)


def test_transport_retry_must_reuse_the_identical_request_payload() -> None:
    slot = _slot(1)
    slot.transport_attempt_payload_sha256s = ["c" * 64] * 3
    assert len(slot.transport_attempt_payload_sha256s) == 3

    with pytest.raises(ValidationError, match="retry changed"):
        ProposalSlot(
            **{
                **slot.model_dump(),
                "transport_attempt_payload_sha256s": ["c" * 64, "d" * 64],
            }
        )


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


def test_inference_uses_ten_block_pairs_and_one_sided_ci_lower_bound() -> None:
    protocol = _protocol()

    result = analyze_p2_r1(_input(protocol), protocol)

    assert len(result.paired_normalized_deltas) == 10
    assert result.primary_median_delta == 0.0
    assert result.primary_one_sided_95_percent_lower_bound == 0.0
    assert result.primary_gate_passed is True
    assert result.non_inferiority_assessment == "NON_INFERIORITY_SUPPORTED"


def test_state_isolation_and_equal_initial_baseline_are_enforced() -> None:
    protocol = _protocol()
    payload = _input(protocol)
    payload.runs[0].state_namespace = "shared-state"

    with pytest.raises(ValueError, match="state namespace"):
        analyze_p2_r1(payload, protocol)

    mismatch = _input(
        protocol,
        baseline_overrides={(1, "native"): 1.0001},
    )
    result = analyze_p2_r1(mismatch, protocol)
    assert result.statistical_eligibility == "NOT_EVALUABLE_DATA"
    assert "BASELINE_MISMATCH:block-01" in result.ineligibility_reasons


def test_terminal_taxonomy_partitions_patch_compile_and_evaluator_failures() -> None:
    protocol = _protocol()
    overrides = {
        (1, "native", 1): _terminal_slot(1, "MODEL_RESPONSE_MISSING"),
        (1, "native", 2): _terminal_slot(2, "MATERIALIZATION_FAILED"),
        (1, "native", 3): _terminal_slot(3, "COMPILE_FAILED"),
        (1, "native", 4): _terminal_slot(4, "EVALUATOR_NOT_REACHED"),
        (1, "native", 5): _terminal_slot(5, "EVALUATOR_INVALID"),
    }

    result = analyze_p2_r1(
        _input(protocol, slot_overrides=overrides), protocol
    )
    classes = result.per_run_terminal_classes["block-01-native"]

    assert sum(classes.values()) == 5
    assert classes["MODEL_RESPONSE_MISSING"] == 1
    assert classes["MATERIALIZATION_FAILED"] == 1
    assert classes["COMPILE_FAILED"] == 1
    assert classes["EVALUATOR_NOT_REACHED"] == 1
    assert classes["EVALUATOR_INVALID"] == 1


def test_nan_zero_evaluator_denominator_and_all_carry_forward_are_safe() -> None:
    with pytest.raises(ValidationError, match="finite score"):
        _slot(1, score=float("nan"))

    protocol = _protocol()
    never_reached = {
        (block, arm, slot): _terminal_slot(slot, "EVALUATOR_NOT_REACHED")
        for block in range(1, 11)
        for arm in ("official", "native")
        for slot in range(1, 6)
    }
    result = analyze_p2_r1(
        _input(protocol, slot_overrides=never_reached), protocol
    )
    assert result.statistical_eligibility == "NOT_EVALUABLE_DATA"
    assert result.evaluator_invalid_rate_by_arm == {"official": 0.0, "native": 0.0}

    all_invalid = {
        (block, arm, slot): _terminal_slot(slot, "EVALUATOR_INVALID")
        for block in range(1, 11)
        for arm in ("official", "native")
        for slot in range(1, 6)
    }
    carried = analyze_p2_r1(
        _input(protocol, slot_overrides=all_invalid), protocol
    )
    assert carried.primary_median_delta == 0.0
    assert carried.statistical_eligibility == "NOT_EVALUABLE_DATA"
    assert "NO_EVALUATOR_VALID_PROPOSAL:native" in carried.ineligibility_reasons
