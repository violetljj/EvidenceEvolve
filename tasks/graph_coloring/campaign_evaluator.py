from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evidence_evolve.benchmarks.models import DatasetVisibility, GraphInstanceSpec
from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.models import (
    EvaluationInput,
    MechanicsStatus,
    ScientificOutcome,
)
from tasks.graph_coloring.evaluator import evaluate_split


INSTANCE_MANIFEST = Path("benchmarks/graph_coloring/instances_v0.json")


def development_instances(repo_root: Path) -> list[GraphInstanceSpec]:
    payload = json.loads((repo_root / INSTANCE_MANIFEST).read_text(encoding="utf-8"))
    return [GraphInstanceSpec.model_validate(item) for item in payload["development"]]


def evaluate_development(candidate_path: Path, repo_root: Path) -> dict[str, Any]:
    try:
        split = evaluate_split(
            candidate_path,
            development_instances(repo_root),
            visibility=DatasetVisibility.DEVELOPMENT,
            trial_seed=0,
        )
    except Exception as exc:
        return {
            "mechanics_status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "metrics": {
                "invalid_coloring_rate": 1.0,
                "nonreproducible_rate": 1.0,
                "mean_color_count": 1000000000.0,
                "relative_improvement": -1.0,
            },
            "controls": {
                "coloring_valid": False,
                "deterministic_solver": False,
                "development_only_evaluation": True,
            },
            "improved": False,
            "seed": 0,
        }
    controls = {
        "coloring_valid": split.valid_rate == 1.0,
        "deterministic_solver": split.reproducibility_rate == 1.0,
        "development_only_evaluation": True,
    }
    return {
        "mechanics_status": "PASS",
        "metrics": {
            "invalid_coloring_rate": 1.0 - split.valid_rate,
            "nonreproducible_rate": 1.0 - split.reproducibility_rate,
            "mean_color_count": (
                split.mean_candidate_colors
                if split.mean_candidate_colors is not None
                else 1000000000.0
            ),
            "relative_improvement": split.mean_relative_improvement,
        },
        "controls": controls,
        "improved": split.positive_relative_improvement > 0.0 and all(controls.values()),
        "seed": 0,
        "development_instance_count": split.instance_count,
        "failure_reasons": split.failure_reasons,
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
    outcome = (
        ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
        if mechanics is MechanicsStatus.FAIL
        else ScientificOutcome.POSITIVE_HEADROOM
        if bool(raw.get("improved", False))
        else ScientificOutcome.VALID_NEGATIVE
    )
    return EvaluationInput(
        contract_sha256=contract_sha256,
        candidate=candidate.acquisition.candidate,
        stage=candidate.stage,
        changed_files=changed_files,
        mechanics_status=mechanics,
        data_eligible=True,
        metrics={str(key): float(value) for key, value in raw["metrics"].items()},
        controls=controls,
        scientific_outcome=outcome,
    )
