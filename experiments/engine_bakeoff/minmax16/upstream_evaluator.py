# Source: https://github.com/skydiscover-ai/skydiscover
# Commit: 8a840394e19ee4bfb3fb0a62762b902561a7efeb
# Upstream path: benchmarks/math/minimizing_max_min_dist/2/evaluator/evaluator.py

import os
import sys
import time
from importlib import __import__

import numpy as np
import scipy as sp


NUM_POINTS = 16
DIMENSION = 2
BENCHMARK = 1 / 12.889266112


def evaluate(program_path: str):
    try:
        abs_program_path = os.path.abspath(program_path)
        program_dir = os.path.dirname(abs_program_path)
        module_name = os.path.splitext(os.path.basename(program_path))[0]

        try:
            sys.path.insert(0, program_dir)
            program = __import__(module_name)
            start_time = time.time()
            points = program.min_max_dist_dim2_16()
            end_time = time.time()
            eval_time = end_time - start_time
        except Exception as err:
            raise err
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
