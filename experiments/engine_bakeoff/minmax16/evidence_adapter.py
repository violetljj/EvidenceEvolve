from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

from evidence_evolve.discovery.autonomous import AutonomousEvaluationContext
from evidence_evolve.discovery.campaign import EvaluationRun
from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.models import (
    EvaluationInput,
    MechanicsStatus,
    ScientificOutcome,
)
from evidence_evolve.worktrees import WorktreeManager


CANDIDATE_PATH = Path("experiments/engine_bakeoff/minmax16/initial.py")
EVALUATOR_PATH = Path("experiments/engine_bakeoff/minmax16/evaluate.py")
BASELINE_SCORE = 0.01979112384555867


def _single_evaluation(
    candidate_path: Path,
    evaluator_path: Path,
    python_executable: Path,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ee-minmax16-eval-") as result_dir:
        completed = subprocess.run(
            [
                str(python_executable),
                str(evaluator_path),
                "--program_path",
                str(candidate_path),
                "--results_dir",
                result_dir,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        metrics_path = Path(result_dir) / "metrics.json"
        correct_path = Path(result_dir) / "correct.json"
        if completed.returncode != 0 or not metrics_path.is_file() or not correct_path.is_file():
            return {
                "correct": False,
                "error": completed.stderr or completed.stdout or f"exit {completed.returncode}",
                "metrics": {},
            }
        return {
            "correct": bool(json.loads(correct_path.read_text(encoding="utf-8"))["correct"]),
            "error": json.loads(correct_path.read_text(encoding="utf-8")).get("error"),
            "metrics": json.loads(metrics_path.read_text(encoding="utf-8")),
        }


def evaluate_program_twice(
    candidate_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    evaluator_path = repo_root / EVALUATOR_PATH
    python_executable = Path("/root/autodl-tmp/EvidenceEvolve/.venv/bin/python")
    first = _single_evaluation(candidate_path, evaluator_path, python_executable)
    second = _single_evaluation(candidate_path, evaluator_path, python_executable)
    first_score = float(first.get("metrics", {}).get("combined_score", 0.0))
    second_score = float(second.get("metrics", {}).get("combined_score", 0.0))
    first_ratio = float(
        first.get("metrics", {}).get("public", {}).get("min_max_ratio", 0.0)
    )
    second_ratio = float(
        second.get("metrics", {}).get("public", {}).get("min_max_ratio", 0.0)
    )
    correct = bool(first.get("correct")) and bool(second.get("correct"))
    reproducible = correct and first_score == second_score and first_ratio == second_ratio
    return {
        "mechanics_status": "PASS" if correct else "FAIL",
        "metrics": {
            "invalid_candidate_rate": 0.0 if correct else 1.0,
            "nonreproducible_rate": 0.0 if reproducible else 1.0,
            "combined_score": first_score,
            "min_max_ratio": first_ratio,
            "relative_improvement": first_score / BASELINE_SCORE - 1.0,
        },
        "controls": {
            "upstream_evaluator_completed": correct,
            "finite_score": correct,
            "deterministic_candidate": reproducible,
            "public_development_only": True,
        },
        "improved": correct and reproducible and first_score > BASELINE_SCORE,
        "first": first,
        "second": second,
    }


def evaluate_candidate(context: AutonomousEvaluationContext) -> EvaluationRun:
    candidate_path = context.worktree / CANDIDATE_PATH
    changed_files = WorktreeManager(context.repo_root).changed_files(
        context.worktree, context.genetic_parent_commit
    )
    started = perf_counter()
    raw = evaluate_program_twice(candidate_path, context.repo_root)
    elapsed = perf_counter() - started
    mechanics = MechanicsStatus(str(raw["mechanics_status"]))
    outcome = (
        ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
        if mechanics is MechanicsStatus.FAIL
        else ScientificOutcome.POSITIVE_HEADROOM
        if bool(raw["improved"])
        else ScientificOutcome.VALID_NEGATIVE
    )
    evaluation = EvaluationInput(
        contract_sha256=context.contract.lock.content_sha256,
        candidate=context.candidate.acquisition.candidate,
        stage=context.candidate.stage,
        changed_files=changed_files,
        mechanics_status=mechanics,
        data_eligible=True,
        metrics={str(key): float(value) for key, value in raw["metrics"].items()},
        controls={str(key): bool(value) for key, value in raw["controls"].items()},
        scientific_outcome=outcome,
    )
    patch = subprocess.run(
        ["git", "diff", "--binary", context.genetic_parent_commit, "--"],
        cwd=context.worktree,
        check=True,
        capture_output=True,
    ).stdout
    candidate_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=context.worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return EvaluationRun(
        evaluation=evaluation,
        command=[str(context.repo_root / EVALUATOR_PATH), "two-independent-processes"],
        elapsed_seconds=elapsed,
        seed=0,
        candidate_commit=candidate_commit,
        patch_sha256=sha256_bytes(patch),
    )
