"""Externally timed R4 Set Cover evaluator with diagnostic solver telemetry."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Iterable

from tasks.algotune_set_cover.common import (
    PROBLEM_SIZE,
    _fresh,
    _load_solver,
    _valid_solution,
    generate_problem,
    reference_solution,
    solve_reference,
)


PROFILE_KEYS = (
    "node_expansions",
    "bound_time_ns",
    "cache_time_ns",
    "reduction_ratio",
)


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _snapshot(solver: Any) -> dict[str, float]:
    callback = getattr(solver, "profile_snapshot", None)
    if not callable(callback):
        return {}
    raw = callback()
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for key in PROFILE_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = max(float(value), 0.0)
    return result


def evaluate_candidate_profiled(
    candidate_path: str | Path,
    seeds: Iterable[int],
    repeats: int,
    *,
    problem_size: int = PROBLEM_SIZE,
) -> dict[str, Any]:
    """Evaluate exactness and latency; self-reported counters have no gate authority."""

    seed_list = [int(seed) for seed in seeds]
    started = time.perf_counter()
    try:
        solver = _load_solver(Path(candidate_path).resolve())
    except Exception as exc:
        return {
            "raw_speedup": 0.0,
            "valid_rate": 0.0,
            "correct": False,
            "instance_count": len(seed_list),
            "elapsed_seconds": time.perf_counter() - started,
            "failure": f"LOAD:{type(exc).__name__}:{exc}",
            "telemetry_available": False,
        }

    candidate_times: list[int] = []
    reference_times: list[int] = []
    valid_instances = 0
    failure = ""
    telemetry_totals = {key: 0.0 for key in PROFILE_KEYS}
    telemetry_available = True
    for seed in seed_list:
        problem = tuple(tuple(subset) for subset in generate_problem(problem_size, seed))
        reference = reference_solution(problem)
        try:
            proposed = solver.solve(_fresh(problem))
            if not _valid_solution(problem, proposed, len(reference)):
                failure = f"INVALID_SOLUTION:seed={seed}"
                break
            valid_instances += 1
            for _ in range(repeats):
                before = time.perf_counter_ns()
                solve_reference(problem)
                reference_times.append(time.perf_counter_ns() - before)
                before = time.perf_counter_ns()
                timed = solver.solve(_fresh(problem))
                candidate_times.append(time.perf_counter_ns() - before)
                if not _valid_solution(problem, timed, len(reference)):
                    raise ValueError("timed solve returned an invalid solution")
            snapshot = _snapshot(solver)
            telemetry_available = telemetry_available and (
                set(snapshot) == set(PROFILE_KEYS)
            )
            for key, value in snapshot.items():
                telemetry_totals[key] += value
        except Exception as exc:
            failure = f"RUN:seed={seed}:{type(exc).__name__}:{exc}"
            break

    correct = bool(seed_list) and valid_instances == len(seed_list)
    candidate_ns = sum(candidate_times)
    reference_ns = sum(reference_times)
    telemetry_observations = len(seed_list) if telemetry_available else 0
    reduction_ratio = (
        telemetry_totals["reduction_ratio"] / telemetry_observations
        if telemetry_observations
        else 0.0
    )
    return {
        "combined_score": reference_ns / candidate_ns if correct and candidate_ns else 0.0,
        "raw_speedup": reference_ns / candidate_ns if correct and candidate_ns else 0.0,
        "valid_rate": valid_instances / len(seed_list) if seed_list else 0.0,
        "correct": correct,
        "instance_count": len(seed_list),
        "candidate_time_ns": candidate_ns,
        "reference_time_ns": reference_ns,
        "wall_time_ns": float(candidate_ns),
        "wall_time_p50_ns": _percentile(candidate_times, 0.50),
        "wall_time_p95_ns": _percentile(candidate_times, 0.95),
        "wall_time_p99_ns": _percentile(candidate_times, 0.99),
        "node_expansions": telemetry_totals["node_expansions"],
        "bound_time_ns": telemetry_totals["bound_time_ns"],
        "cache_time_ns": telemetry_totals["cache_time_ns"],
        "reduction_ratio": reduction_ratio,
        "telemetry_available": telemetry_available,
        "telemetry_scientific_authority": "NONE_DIAGNOSTIC_ONLY",
        "elapsed_seconds": time.perf_counter() - started,
        "failure": failure,
        "worker_count": 1,
    }


__all__ = ["PROFILE_KEYS", "evaluate_candidate_profiled"]
