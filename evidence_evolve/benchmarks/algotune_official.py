from __future__ import annotations

import copy
import importlib.util
import os
import sys
import time
import types
import uuid
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Empty
from typing import Any, Callable, Iterable

from multiprocessing import get_all_start_methods, get_context


@dataclass(frozen=True)
class OfficialTaskSpec:
    name: str
    class_name: str
    problem_size: int
    source_path: str


def _install_base_stub() -> None:
    package = sys.modules.get("AlgoTuneTasks")
    if package is None:
        package = types.ModuleType("AlgoTuneTasks")
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules["AlgoTuneTasks"] = package
    base = types.ModuleType("AlgoTuneTasks.base")

    class Task:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    def register_task(_name: str) -> Any:
        return lambda candidate: candidate

    base.Task = Task
    base.register_task = register_task
    sys.modules["AlgoTuneTasks.base"] = base


def load_task(path: str | Path, class_name: str) -> Any:
    _install_base_stub()
    source = Path(path).resolve()
    module_name = f"ee_algotune_official_{uuid.uuid4().hex}"
    module_spec = importlib.util.spec_from_file_location(module_name, source)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot load AlgoTune task source: {source}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    task_type = getattr(module, class_name, None)
    if not isinstance(task_type, type):
        raise TypeError(f"candidate does not define {class_name}")
    task = task_type()
    if not all(callable(getattr(task, name, None)) for name in ("solve",)):
        raise TypeError("candidate task class must define solve(problem)")
    return task


def _serial(
    candidate_path: str,
    task_spec: OfficialTaskSpec,
    seeds: list[int],
    repeats: int,
) -> dict[str, Any]:
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "1"
    started = time.perf_counter()
    try:
        candidate = load_task(candidate_path, task_spec.class_name)
        oracle = load_task(task_spec.source_path, task_spec.class_name)
    except Exception as exc:
        return {"valid": 0, "count": len(seeds), "candidate_ns": 0, "reference_ns": 0,
                "failure": f"LOAD:{type(exc).__name__}:{exc}"}
    candidate_ns = 0
    reference_ns = 0
    valid = 0
    failure = ""
    for seed in seeds:
        try:
            problem = oracle.generate_problem(task_spec.problem_size, random_seed=seed)
            reference_times: list[int] = []
            candidate_times: list[int] = []
            proposed: Any = None
            for _ in range(repeats):
                before = time.perf_counter_ns()
                oracle.solve(copy.deepcopy(problem))
                reference_times.append(time.perf_counter_ns() - before)
                before = time.perf_counter_ns()
                proposed = candidate.solve(copy.deepcopy(problem))
                candidate_times.append(time.perf_counter_ns() - before)
            if not oracle.is_solution(copy.deepcopy(problem), proposed):
                failure = f"INVALID_SOLUTION:seed={seed}"
                break
            valid += 1
            reference_ns += min(reference_times)
            candidate_ns += min(candidate_times)
        except Exception as exc:
            failure = f"RUN:seed={seed}:{type(exc).__name__}:{exc}"
            break
    return {
        "valid": valid,
        "count": len(seeds),
        "candidate_ns": candidate_ns,
        "reference_ns": reference_ns,
        "failure": failure,
        "elapsed_seconds": time.perf_counter() - started,
    }


def evaluate_official_candidate(
    candidate_path: str | Path,
    task_spec: OfficialTaskSpec,
    seeds: Iterable[int],
    *,
    repeats: int,
    workers: int,
) -> dict[str, Any]:
    seed_list = [int(seed) for seed in seeds]
    worker_count = min(max(workers, 1), max(len(seed_list), 1))
    chunks = [seed_list[index::worker_count] for index in range(worker_count)]
    started = time.perf_counter()
    if worker_count == 1:
        parts = [_serial(str(candidate_path), task_spec, seed_list, repeats)]
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            parts = list(
                pool.map(
                    _serial,
                    [str(Path(candidate_path).resolve())] * worker_count,
                    [task_spec] * worker_count,
                    chunks,
                    [repeats] * worker_count,
                )
            )
    valid = sum(int(part["valid"]) for part in parts)
    candidate_ns = sum(int(part["candidate_ns"]) for part in parts)
    reference_ns = sum(int(part["reference_ns"]) for part in parts)
    correct = bool(seed_list) and valid == len(seed_list)
    failure = next((str(part["failure"]) for part in parts if part["failure"]), "")
    raw_speedup = reference_ns / candidate_ns if correct and candidate_ns else 0.0
    return {
        "combined_score": raw_speedup if correct else 0.0,
        "raw_speedup": raw_speedup,
        "valid_rate": valid / len(seed_list) if seed_list else 0.0,
        "correct": correct,
        "instance_count": len(seed_list),
        "candidate_time_ns": candidate_ns,
        "reference_time_ns": reference_ns,
        "elapsed_seconds": time.perf_counter() - started,
        "failure": failure,
        "worker_count": worker_count,
    }


def _cold_trial(
    candidate_path: str,
    task_spec: OfficialTaskSpec,
    seed: int,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    report = progress or (lambda _stage: None)
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "1"
    report("SETUP")
    oracle = load_task(task_spec.source_path, task_spec.class_name)
    problem = oracle.generate_problem(task_spec.problem_size, random_seed=seed)
    report("REFERENCE")
    before = time.perf_counter_ns()
    reference_solver = load_task(task_spec.source_path, task_spec.class_name)
    reference_solver.solve(copy.deepcopy(problem))
    reference_ns = time.perf_counter_ns() - before
    report("CANDIDATE")
    before = time.perf_counter_ns()
    candidate = load_task(candidate_path, task_spec.class_name)
    proposed = candidate.solve(copy.deepcopy(problem))
    candidate_ns = time.perf_counter_ns() - before
    report("VERIFY")
    valid = oracle.is_solution(copy.deepcopy(problem), proposed)
    return {
        "seed": seed,
        "status": "PASS" if valid else "INVALID_SOLUTION",
        "candidate_ns": candidate_ns,
        "reference_ns": reference_ns,
    }


def _cold_entry(
    index: int,
    candidate_path: str,
    task_spec: OfficialTaskSpec,
    seed: int,
    queue: Any,
) -> None:
    stage = "SETUP"

    def report(next_stage: str) -> None:
        nonlocal stage
        stage = next_stage
        queue.put((index, "PROGRESS", {}, next_stage))

    try:
        row = _cold_trial(candidate_path, task_spec, seed, report)
        queue.put((index, "RESULT", row, ""))
    except BaseException as exc:
        queue.put((index, "ERROR", {}, f"{stage}:{type(exc).__name__}:{exc}"))


def evaluate_official_candidate_cold(
    candidate_path: str | Path,
    task_spec: OfficialTaskSpec,
    seeds: Iterable[int],
    *,
    repeats: int,
    workers: int,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Evaluate every timed candidate call in a new killable process."""
    trials = [int(seed) for seed in seeds for _ in range(repeats)]
    start_method = "fork" if "fork" in get_all_start_methods() else "spawn"
    context = get_context(start_method)
    queue = context.Queue()
    pending = deque(enumerate(trials))
    active: dict[int, tuple[Any, float, int, str]] = {}
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    worker_count = min(max(workers, 1), max(len(trials), 1))
    while pending or active:
        while pending and len(active) < worker_count:
            index, seed = pending.popleft()
            process = context.Process(
                target=_cold_entry,
                args=(index, str(Path(candidate_path).resolve()), task_spec, seed, queue),
            )
            process.start()
            active[index] = (process, time.monotonic(), seed, "STARTUP")
        try:
            index, event, row, detail = queue.get(timeout=0.02)
            if event == "PROGRESS":
                entry = active.get(index)
                if entry is not None:
                    active[index] = (entry[0], time.monotonic(), entry[2], detail)
                continue
            entry = active.pop(index, None)
            if entry is None:
                # A killed process may flush a late message after its timeout
                # row was recorded. Never count one trial twice.
                continue
            entry[0].join()
            if event == "RESULT":
                rows.append(row)
            else:
                stage, _separator, error = detail.partition(":")
                rows.append({
                    "seed": trials[index], "status": f"ERROR_{stage}", "error": error,
                })
        except Empty:
            pass
        now = time.monotonic()
        for index, (process, process_started, seed, stage) in list(active.items()):
            stage_timeout = max(timeout_seconds, 10.0) if stage == "STARTUP" else timeout_seconds
            if now - process_started <= stage_timeout:
                continue
            process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join()
            active.pop(index)
            rows.append({"seed": seed, "status": f"TIMEOUT_{stage}"})
    queue.close()
    passed = [row for row in rows if row["status"] == "PASS"]
    by_seed: dict[int, list[dict[str, Any]]] = {}
    for row in passed:
        by_seed.setdefault(int(row["seed"]), []).append(row)
    candidate_ns = sum(min(int(row["candidate_ns"]) for row in items) for items in by_seed.values())
    reference_ns = sum(min(int(row["reference_ns"]) for row in items) for items in by_seed.values())
    correct = len(passed) == len(trials) and bool(trials)
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    failure_examples = [
        {key: value for key, value in row.items() if key not in {"candidate_ns", "reference_ns"}}
        for row in rows
        if row["status"] != "PASS"
    ][:10]
    return {
        "combined_score": reference_ns / candidate_ns if correct and candidate_ns else 0.0,
        "raw_speedup": reference_ns / candidate_ns if correct and candidate_ns else 0.0,
        "valid_rate": len(passed) / len(trials) if trials else 0.0,
        "correct": correct,
        "instance_count": len(set(trials)),
        "trial_count": len(trials),
        "candidate_time_ns": candidate_ns,
        "reference_time_ns": reference_ns,
        "elapsed_seconds": time.perf_counter() - started,
        "failure": "" if correct else json_status(status_counts),
        "failure_examples": failure_examples,
        "status_counts": status_counts,
        "worker_count": worker_count,
        "fresh_process_per_solver_call": True,
    }


def json_status(status_counts: dict[str, int]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(status_counts.items()))
