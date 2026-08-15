from __future__ import annotations

import importlib.util
import os
import random
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from pysat.card import CardEnc, EncType
from pysat.formula import CNF
from pysat.solvers import Solver as SatSolver


PROBLEM_SIZE = 52
DEVELOPMENT_SEEDS = tuple(range(100))


def generate_problem(n: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    subset_count = rng.randint(n, 2 * n)
    subsets: list[list[int]] = []
    for _ in range(subset_count):
        size = rng.randint(1, max(n // 2, 1))
        subsets.append(sorted(rng.sample(range(1, n + 1), k=size)))
    covered = {element for subset in subsets for element in subset}
    subsets.extend([[element] for element in range(1, n + 1) if element not in covered])
    return subsets


def _sat_formula(problem: tuple[tuple[int, ...], ...], bound: int) -> CNF:
    universe = sorted({element for subset in problem for element in subset})
    cnf = CNF()
    for element in universe:
        covers = [
            index + 1
            for index, subset in enumerate(problem)
            if element in subset
        ]
        cnf.append(covers or [1, -1])
    cnf.extend(
        CardEnc.atmost(
            lits=list(range(1, len(problem) + 1)),
            bound=bound,
            encoding=EncType.seqcounter,
        ).clauses
    )
    return cnf


def solve_reference(problem: tuple[tuple[int, ...], ...]) -> tuple[int, ...]:
    left, right = 1, len(problem) + 1
    best: tuple[int, ...] = ()
    while left < right:
        midpoint = (left + right) // 2
        with SatSolver(name="Minicard", bootstrap_with=_sat_formula(problem, midpoint)) as sat:
            satisfiable = sat.solve()
            model = sat.get_model() if satisfiable else None
        if model is None:
            left = midpoint + 1
            continue
        best = tuple(
            index + 1
            for index in range(len(problem))
            if index + 1 in model
        )
        right = len(best)
    return best


@lru_cache(maxsize=4096)
def reference_solution(problem: tuple[tuple[int, ...], ...]) -> tuple[int, ...]:
    return solve_reference(problem)


def _load_solver(candidate_path: Path) -> Any:
    module_name = f"ee_algotune_candidate_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, candidate_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load candidate: {candidate_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    solver_type = getattr(module, "Solver", None)
    if solver_type is None:
        raise AttributeError("candidate must define class Solver")
    solver = solver_type()
    if not callable(getattr(solver, "solve", None)):
        raise AttributeError("Solver must define solve(problem)")
    return solver


def _fresh(problem: tuple[tuple[int, ...], ...]) -> list[list[int]]:
    return [list(subset) for subset in problem]


def _valid_solution(
    problem: tuple[tuple[int, ...], ...],
    solution: Any,
    optimal_size: int,
) -> bool:
    if not isinstance(solution, (list, tuple)) or len(solution) != optimal_size:
        return False
    if any(isinstance(index, bool) or not isinstance(index, int) for index in solution):
        return False
    if any(index < 1 or index > len(problem) for index in solution):
        return False
    if len(set(solution)) != len(solution):
        return False
    covered = {
        element
        for index in solution
        for element in problem[index - 1]
    }
    universe = {element for subset in problem for element in subset}
    return covered == universe


def _evaluate_candidate_serial(
    candidate_path: str | Path,
    seeds: list[int],
    *,
    repeats: int,
    problem_size: int = PROBLEM_SIZE,
) -> dict[str, Any]:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    started = time.perf_counter()
    seed_list = [int(seed) for seed in seeds]
    try:
        solver = _load_solver(Path(candidate_path).resolve())
    except Exception as exc:
        return {
            "combined_score": 0.0,
            "raw_speedup": 0.0,
            "valid_rate": 0.0,
            "correct": False,
            "instance_count": len(seed_list),
            "elapsed_seconds": time.perf_counter() - started,
            "failure": f"LOAD:{type(exc).__name__}:{exc}",
        }

    candidate_ns = 0
    reference_ns = 0
    valid_instances = 0
    failure = ""
    for seed in seed_list:
        problem = tuple(tuple(subset) for subset in generate_problem(problem_size, seed))
        reference = reference_solution(problem)
        try:
            proposed = solver.solve(_fresh(problem))
            if not _valid_solution(problem, proposed, len(reference)):
                failure = f"INVALID_SOLUTION:seed={seed}"
                break
            valid_instances += 1

            candidate_times: list[int] = []
            reference_times: list[int] = []
            for _ in range(repeats):
                before = time.perf_counter_ns()
                solve_reference(problem)
                reference_times.append(time.perf_counter_ns() - before)

                before = time.perf_counter_ns()
                timed = solver.solve(_fresh(problem))
                candidate_times.append(time.perf_counter_ns() - before)
                if not _valid_solution(problem, timed, len(reference)):
                    raise ValueError("timed solve returned an invalid solution")
            reference_ns += min(reference_times)
            candidate_ns += min(candidate_times)
        except Exception as exc:
            failure = f"RUN:seed={seed}:{type(exc).__name__}:{exc}"
            break

    valid_rate = valid_instances / len(seed_list) if seed_list else 0.0
    correct = valid_instances == len(seed_list) and bool(seed_list)
    raw_speedup = (
        reference_ns / candidate_ns
        if correct and candidate_ns > 0 and reference_ns > 0
        else 0.0
    )
    return {
        "combined_score": raw_speedup if correct else 0.0,
        "raw_speedup": raw_speedup,
        "valid_rate": valid_rate,
        "correct": correct,
        "instance_count": len(seed_list),
        "candidate_time_ns": candidate_ns,
        "reference_time_ns": reference_ns,
        "elapsed_seconds": time.perf_counter() - started,
        "failure": failure,
    }


def evaluate_candidate(
    candidate_path: str | Path,
    seeds: Iterable[int],
    *,
    repeats: int,
    problem_size: int = PROBLEM_SIZE,
) -> dict[str, Any]:
    seed_list = [int(seed) for seed in seeds]
    requested_workers = int(os.environ.get("EE_ALGOTUNE_WORKERS", "1"))
    worker_count = min(max(requested_workers, 1), max(len(seed_list), 1))
    if worker_count == 1:
        result = _evaluate_candidate_serial(
            candidate_path,
            seed_list,
            repeats=repeats,
            problem_size=problem_size,
        )
        result["worker_count"] = 1
        return result

    started = time.perf_counter()
    chunks = [seed_list[index::worker_count] for index in range(worker_count)]
    with ProcessPoolExecutor(max_workers=worker_count) as pool:
        futures = [
            pool.submit(
                _evaluate_candidate_serial,
                str(Path(candidate_path).resolve()),
                chunk,
                repeats=repeats,
                problem_size=problem_size,
            )
            for chunk in chunks
        ]
        parts = [future.result() for future in futures]
    valid_instances = sum(
        round(float(part["valid_rate"]) * int(part["instance_count"]))
        for part in parts
    )
    correct = bool(seed_list) and valid_instances == len(seed_list)
    candidate_ns = sum(int(part.get("candidate_time_ns", 0)) for part in parts)
    reference_ns = sum(int(part.get("reference_time_ns", 0)) for part in parts)
    failure = next((str(part["failure"]) for part in parts if part.get("failure")), "")
    raw_speedup = (
        reference_ns / candidate_ns
        if correct and candidate_ns > 0 and reference_ns > 0
        else 0.0
    )
    return {
        "combined_score": raw_speedup if correct else 0.0,
        "raw_speedup": raw_speedup,
        "valid_rate": valid_instances / len(seed_list) if seed_list else 0.0,
        "correct": correct,
        "instance_count": len(seed_list),
        "candidate_time_ns": candidate_ns,
        "reference_time_ns": reference_ns,
        "elapsed_seconds": time.perf_counter() - started,
        "failure": failure,
        "worker_count": worker_count,
    }
