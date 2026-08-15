from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evidence_evolve.benchmarks.models import DatasetVisibility, GraphInstanceSpec
from tasks.graph_coloring.evaluator import evaluate_split


INSTANCE_MANIFEST = REPO_ROOT / "benchmarks/graph_coloring/instances_v0.json"


def main(program_path: str, results_dir: str) -> None:
    destination = Path(results_dir)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(INSTANCE_MANIFEST.read_text(encoding="utf-8"))
        instances = [
            GraphInstanceSpec.model_validate(item) for item in payload["development"]
        ]
        result = evaluate_split(
            Path(program_path).resolve(),
            instances,
            visibility=DatasetVisibility.DEVELOPMENT,
            trial_seed=0,
        )
        correct = result.valid_rate == 1.0 and result.reproducibility_rate == 1.0
        metrics = {
            "combined_score": (
                result.mean_relative_improvement if correct else -1.0
            ),
            "public": {
                "mean_color_count": result.mean_candidate_colors,
                "mean_relative_improvement": result.mean_relative_improvement,
                "valid_rate": result.valid_rate,
                "reproducibility_rate": result.reproducibility_rate,
            },
            "private": {
                "instance_count": result.instance_count,
                "failure_count": len(result.failure_reasons),
            },
        }
        error = None if correct else ";".join(result.failure_reasons) or "invalid"
    except Exception as exc:
        correct = False
        error = f"{type(exc).__name__}: {exc}"
        metrics = {
            "combined_score": -1.0,
            "public": {
                "mean_color_count": 1000000000.0,
                "mean_relative_improvement": -1.0,
                "valid_rate": 0.0,
                "reproducibility_rate": 0.0,
            },
            "private": {"instance_count": 0, "failure_count": 1},
        }

    (destination / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (destination / "correct.json").write_text(
        json.dumps({"correct": correct, "error": error}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"correct": correct, "error": error, "metrics": metrics}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--program_path", default="initial.py")
    parser.add_argument("--results_dir", default="results")
    args = parser.parse_args()
    main(args.program_path, args.results_dir)
