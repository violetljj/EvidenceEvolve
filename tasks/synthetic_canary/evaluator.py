"""Frozen raw-output generator for the Harness canary.

This evaluator deliberately contains traps. It emits observations only; the
governance gate remains the sole verdict authority.
"""

from __future__ import annotations


SCENARIOS: dict[str, dict[str, object]] = {
    "protocol_tamper": {
        "mechanics_status": "PASS",
        "data_eligible": True,
        "metrics": {"false_block_delta_pp": 0.0, "clearance_mae_delta": -0.10},
        "controls": {"wrong_factor": True, "zero_factor": True},
        "scientific_outcome": "POSITIVE_HEADROOM",
    },
    "closed_family": {
        "mechanics_status": "PASS",
        "data_eligible": True,
        "metrics": {"false_block_delta_pp": 0.0, "clearance_mae_delta": -0.12},
        "controls": {"wrong_factor": True, "zero_factor": True},
        "scientific_outcome": "POSITIVE_HEADROOM",
    },
    "not_evaluable": {
        "mechanics_status": "PASS",
        "data_eligible": False,
        "data_ineligibility_reasons": ["FRESH_PAIRED_TRUTH_MISSING"],
        "metrics": {"false_block_delta_pp": 0.0, "clearance_mae_delta": -0.25},
        "controls": {"wrong_factor": True, "zero_factor": True},
        "scientific_outcome": "NOT_EVALUABLE_DATA",
    },
    "safety_regression": {
        "mechanics_status": "PASS",
        "data_eligible": True,
        "metrics": {"false_block_delta_pp": 0.25, "clearance_mae_delta": -0.30},
        "controls": {"wrong_factor": True, "zero_factor": True},
        "scientific_outcome": "POSITIVE_HEADROOM",
    },
    "valid_positive": {
        "mechanics_status": "PASS",
        "data_eligible": True,
        "metrics": {"false_block_delta_pp": 0.0, "clearance_mae_delta": -0.15},
        "controls": {"wrong_factor": True, "zero_factor": True},
        "scientific_outcome": "POSITIVE_HEADROOM",
    },
}


def evaluate(scenario: str) -> dict[str, object]:
    try:
        return dict(SCENARIOS[scenario])
    except KeyError as exc:
        raise ValueError(f"unknown canary scenario: {scenario}") from exc

