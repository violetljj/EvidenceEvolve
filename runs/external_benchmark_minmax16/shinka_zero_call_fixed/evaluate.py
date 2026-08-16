from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from importlib import __import__
from pathlib import Path

import numpy as np
import scipy as sp


NUM_POINTS = 16
DIMENSION = 2
BENCHMARK = 1 / 12.889266112


def evaluate(program_path: str) -> dict[str, object]:
    """Execute the pinned SkyDiscover evaluator logic in an isolated process."""
    try:
        abs_program_path = os.path.abspath(program_path)
        program_dir = os.path.dirname(abs_program_path)
        module_name = os.path.splitext(os.path.basename(program_path))[0]
        try:
            sys.path.insert(0, program_dir)
            program = __import__(module_name)
            started = time.time()
            points = program.min_max_dist_dim2_16()
            eval_time = time.time() - started
        finally:
            if program_dir in sys.path:
                sys.path.remove(program_dir)

        if not isinstance(points, np.ndarray):
            points = np.array(points)
        if points.shape != (NUM_POINTS, DIMENSION):
            raise ValueError(
                f"Invalid shapes: points = {points.shape}, expected {(NUM_POINTS, DIMENSION)}"
            )
        pairwise_distances = sp.spatial.distance.pdist(points)
        min_distance = np.min(pairwise_distances)
        max_distance = np.max(pairwise_distances)
        inv_ratio_squared = (
            (min_distance / max_distance) ** 2 if max_distance > 0 else 0
        )
        return {
            "min_max_ratio": float(inv_ratio_squared),
            "combined_score": float(inv_ratio_squared / BENCHMARK),
            "eval_time": float(eval_time),
        }
    except Exception as exc:
        return {"combined_score": 0.0, "error": str(exc)}


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
