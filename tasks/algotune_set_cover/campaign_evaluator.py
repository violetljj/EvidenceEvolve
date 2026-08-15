from __future__ import annotations

from pathlib import Path
from typing import Any

from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.models import EvaluationInput, MechanicsStatus, ScientificOutcome
from tasks.algotune_set_cover.evaluator import evaluate


def evaluate_development(candidate_path: Path) -> dict[str, Any]:
    try:
        result = evaluate(str(candidate_path))
    except Exception as exc:
        return {
            "mechanics_status": "FAIL",
            "metrics": {"invalid_solution_rate": 1.0, "raw_speedup": 0.0},
            "controls": {"candidate_valid": False, "development_only": True},
            "error": f"{type(exc).__name__}:{exc}",
        }
    return {
        "mechanics_status": "PASS",
        "metrics": {
            "invalid_solution_rate": 1.0 - float(result["valid_rate"]),
            "raw_speedup": float(result["raw_speedup"]),
        },
        "controls": {
            "candidate_valid": bool(result["correct"]),
            "development_only": True,
        },
        "error": str(result.get("failure", "")),
    }


def build_evaluation(
    *,
    contract_sha256: str,
    candidate: CampaignCandidate,
    changed_files: list[str],
    raw: dict[str, Any],
) -> EvaluationInput:
    mechanics = MechanicsStatus(str(raw["mechanics_status"]))
    controls = {str(key): bool(value) for key, value in raw["controls"].items()}
    metrics = {str(key): float(value) for key, value in raw["metrics"].items()}
    outcome = (
        ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
        if mechanics is MechanicsStatus.FAIL
        else ScientificOutcome.POSITIVE_HEADROOM
        if all(controls.values()) and metrics["raw_speedup"] > 1.0
        else ScientificOutcome.VALID_NEGATIVE
    )
    return EvaluationInput(
        contract_sha256=contract_sha256,
        candidate=candidate.acquisition.candidate,
        stage=candidate.stage,
        changed_files=changed_files,
        mechanics_status=mechanics,
        data_eligible=True,
        metrics=metrics,
        controls=controls,
        scientific_outcome=outcome,
    )
