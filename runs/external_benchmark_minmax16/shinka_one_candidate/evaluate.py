from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from upstream_evaluator import evaluate


def main(program_path: str, results_dir: str) -> None:
    destination = Path(results_dir)
    destination.mkdir(parents=True, exist_ok=True)
    raw = evaluate(program_path)
    score = raw.get("combined_score")
    correct = (
        "error" not in raw
        and isinstance(score, (int, float))
        and math.isfinite(float(score))
    )
    metrics = {
        "combined_score": float(score) if correct else 0.0,
        "public": {
            "min_max_ratio": float(raw.get("min_max_ratio", 0.0)),
            "eval_time": float(raw.get("eval_time", 0.0)),
        },
        "private": {},
    }
    error = None if correct else str(raw.get("error", "invalid evaluator result"))
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
    parser.add_argument("--program_path", required=True)
    parser.add_argument("--results_dir", required=True)
    args = parser.parse_args()
    main(args.program_path, args.results_dir)
