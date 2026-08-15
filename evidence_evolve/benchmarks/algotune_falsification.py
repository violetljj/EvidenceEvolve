from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import resource
import secrets
import sys
import time
import uuid
from collections import defaultdict
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from typing import Any, Iterable

from tasks.algotune_set_cover.common import (
    _fresh,
    _valid_solution,
    generate_problem,
    solve_reference,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
ARMS = ("shinka", "ada", "vanilla", "evidence_evolve", "evox")
SHIFT_SIZES = (40, 52, 64, 80, 100)
SHIFT_DENSITIES = (0.08, 0.15, 0.30, 0.50)


@dataclass(frozen=True)
class InstanceTrial:
    candidates: tuple[tuple[str, str], ...]
    seed: int
    repeat: int
    size: int
    density: float | None
    timeout_seconds: float


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def generate_density_problem(n: int, seed: int, density: float) -> list[list[int]]:
    if n < 1 or not 0.0 < density <= 1.0:
        raise ValueError("n and density must describe a non-empty distribution")
    rng = random.Random(seed)
    subset_count = rng.randint(n, 2 * n)
    subsets: list[list[int]] = []
    for _ in range(subset_count):
        subset = [element for element in range(1, n + 1) if rng.random() < density]
        if not subset:
            subset = [rng.randint(1, n)]
        subsets.append(subset)
    covered = {element for subset in subsets for element in subset}
    subsets.extend([[element] for element in range(1, n + 1) if element not in covered])
    return subsets


def _load_solver_type(candidate_path: Path) -> type[Any]:
    module_name = f"ee_cold_candidate_{uuid.uuid4().hex}"
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
    if not isinstance(solver_type, type) or not callable(getattr(solver_type, "solve", None)):
        raise TypeError("candidate must define Solver.solve(problem)")
    return solver_type


def _solve_entry(
    queue: Any,
    kind: str,
    problem: tuple[tuple[int, ...], ...],
    candidate_path: str | None,
) -> None:
    try:
        started = time.perf_counter_ns()
        if kind == "reference":
            result = solve_reference(problem)
        elif kind == "candidate" and candidate_path is not None:
            solver_type = _load_solver_type(Path(candidate_path))
            result = solver_type().solve(_fresh(problem))
        else:
            raise ValueError(f"unknown solve kind: {kind}")
        elapsed = time.perf_counter_ns() - started
        queue.put(("PASS", result, elapsed, ""))
    except BaseException as exc:
        queue.put(("ERROR", None, 0, f"{type(exc).__name__}:{exc}"))


def _hard_solve(
    *,
    kind: str,
    problem: tuple[tuple[int, ...], ...],
    candidate_path: str | None,
    timeout_seconds: float,
) -> tuple[str, Any, int, str]:
    context = get_context("fork")
    queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_solve_entry,
        args=(queue, kind, problem, candidate_path),
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(0.5)
        if process.is_alive():
            process.kill()
            process.join()
        queue.close()
        return "TIMEOUT", None, 0, ""
    try:
        result = queue.get(timeout=1.0)
    except Empty:
        result = ("ERROR", None, 0, f"CHILD_EXIT_{process.exitcode}")
    queue.close()
    return result


def _run_trial(trial: InstanceTrial) -> list[dict[str, Any]]:
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    problem_list = (
        generate_problem(trial.size, trial.seed)
        if trial.density is None
        else generate_density_problem(trial.size, trial.seed, trial.density)
    )
    problem = tuple(tuple(subset) for subset in problem_list)
    base = {
        "seed": trial.seed,
        "repeat": trial.repeat,
        "size": trial.size,
        "density": trial.density,
        "process_id": os.getpid(),
    }
    reference_status, reference, reference_ns, reference_error = _hard_solve(
        kind="reference",
        problem=problem,
        candidate_path=None,
        timeout_seconds=trial.timeout_seconds,
    )
    if reference_status == "TIMEOUT":
        return [
            {**base, "arm": arm, "status": "REFERENCE_TIMEOUT"}
            for arm, _path in trial.candidates
        ]
    if reference_status != "PASS":
        return [
            {
                **base,
                "arm": arm,
                "status": "REFERENCE_ERROR",
                "error": reference_error,
            }
            for arm, _path in trial.candidates
        ]

    candidates = list(trial.candidates)
    random.Random(f"{trial.seed}:{trial.repeat}:{trial.size}:{trial.density}").shuffle(
        candidates
    )
    rows: list[dict[str, Any]] = []
    for arm, candidate_path in candidates:
        arm_base = {**base, "arm": arm}
        candidate_status, proposed, candidate_ns, candidate_error = _hard_solve(
            kind="candidate",
            problem=problem,
            candidate_path=candidate_path,
            timeout_seconds=trial.timeout_seconds,
        )
        if candidate_status == "TIMEOUT":
            rows.append(
                {
                    **arm_base,
                    "status": "CANDIDATE_TIMEOUT",
                    "reference_ns": reference_ns,
                }
            )
            continue
        if candidate_status != "PASS":
            rows.append(
                {
                    **arm_base,
                    "status": "CANDIDATE_ERROR",
                    "reference_ns": reference_ns,
                    "error": candidate_error,
                }
            )
            continue
        valid = _valid_solution(problem, proposed, len(reference))
        rows.append(
            {
                **arm_base,
                "status": "PASS" if valid else "INVALID_SOLUTION",
                "candidate_ns": candidate_ns,
                "reference_ns": reference_ns,
                "solution_size": (
                    len(proposed) if isinstance(proposed, (list, tuple)) else None
                ),
                "optimal_size": len(reference),
            }
        )
    return rows


def _trial_entry(index: int, trial: InstanceTrial, queue: Any) -> None:
    try:
        queue.put((index, _run_trial(trial), ""))
    except BaseException as exc:
        queue.put((index, [], f"{type(exc).__name__}:{exc}"))


def _outer_timeout_rows(trial: InstanceTrial, error: str) -> list[dict[str, Any]]:
    return [
        {
            "arm": arm,
            "seed": trial.seed,
            "repeat": trial.repeat,
            "size": trial.size,
            "density": trial.density,
            "process_id": None,
            "status": "INSTANCE_PROCESS_TIMEOUT" if not error else "INSTANCE_PROCESS_ERROR",
            "error": error,
        }
        for arm, _path in trial.candidates
    ]


def _run_trials_parallel(
    trials: list[InstanceTrial], worker_count: int
) -> list[dict[str, Any]]:
    context = get_context("fork")
    queue = context.Queue()
    pending = deque(enumerate(trials))
    active: dict[int, tuple[Any, float, InstanceTrial]] = {}
    rows: list[dict[str, Any]] = []
    while pending or active:
        while pending and len(active) < worker_count:
            index, trial = pending.popleft()
            process = context.Process(target=_trial_entry, args=(index, trial, queue))
            process.start()
            active[index] = (process, time.monotonic(), trial)
        try:
            index, trial_rows, error = queue.get(timeout=0.02)
            entry = active.pop(index, None)
            if entry is not None:
                entry[0].join()
            rows.extend(trial_rows or _outer_timeout_rows(trials[index], error))
        except Empty:
            pass
        now = time.monotonic()
        for index, (process, started, trial) in list(active.items()):
            hard_limit = (len(trial.candidates) + 1) * (trial.timeout_seconds + 1.0) + 5.0
            if now - started <= hard_limit:
                continue
            process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join()
            active.pop(index)
            rows.extend(_outer_timeout_rows(trial, ""))
    queue.close()
    return rows


def _candidate_lock(source_run: Path, run_dir: Path) -> dict[str, Any]:
    source_lock_path = source_run / "candidate_lock.json"
    source_lock = json.loads(source_lock_path.read_text(encoding="utf-8"))
    candidates: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        result = json.loads(
            (source_run / "arms" / arm / "arm_result.json").read_text(encoding="utf-8")
        )
        path = Path(result["candidate_path"]).resolve()
        digest = _sha256_file(path)
        expected = source_lock["candidates"][arm]["candidate_sha256"]
        if digest != expected:
            raise ValueError(f"source candidate drifted: {arm}")
        candidates[arm] = {"path": str(path), "sha256": digest}
    lock = {
        "schema_version": "1.0",
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "source_run": str(source_run.resolve()),
        "source_candidate_lock_sha256": _sha256_file(source_lock_path),
        "candidates": candidates,
        "falsification_protocol": {
            "fresh_process_per_instance_repeat": True,
            "fresh_process_per_solver_call": True,
            "each_candidate_imported_once_and_solved_once_per_process": True,
            "candidate_order_deterministically_shuffled": True,
            "new_solver_constructed_inside_timed_region": True,
            "candidate_receives_no_untimed_warmup": True,
            "no_retries_or_replacement_samples": True,
        },
    }
    path = run_dir / "candidate_lock.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing["candidates"] != candidates:
            raise ValueError("falsification candidate lock drifted")
        return existing
    _write_json(path, lock)
    return lock


def _fresh_seeds(
    *,
    run_dir: Path,
    source_run: Path,
    cold_count: int,
    shift_count: int,
) -> dict[str, Any]:
    path = run_dir / "seed_receipt.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not (run_dir / "candidate_lock.json").is_file():
        raise ValueError("candidate lock must exist before falsification seeds")
    excluded = set(range(100))
    prior_heldout = source_run / "heldout_seeds.json"
    if prior_heldout.is_file():
        excluded.update(json.loads(prior_heldout.read_text(encoding="utf-8"))["seeds"])

    def draw(count: int) -> list[int]:
        values: set[int] = set()
        while len(values) < count:
            value = secrets.randbits(63)
            if value not in excluded:
                values.add(value)
                excluded.add(value)
        return sorted(values)

    receipt = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_after_candidate_lock": True,
        "generator": "Python secrets.SystemRandom / OS CSPRNG",
        "old_development_and_heldout_seeds_excluded": True,
        "cold_seeds": draw(cold_count),
        "shift_seeds": draw(shift_count),
    }
    _write_json(path, receipt)
    return receipt


def _trials(
    *,
    lock: dict[str, Any],
    seeds: dict[str, Any],
    mode: str,
    cold_repeats: int,
    shift_repeats: int,
    timeout_seconds: float,
) -> list[InstanceTrial]:
    trials: list[InstanceTrial] = []
    candidates = tuple(
        (arm, str(lock["candidates"][arm]["path"])) for arm in ARMS
    )
    if mode in {"cold", "all"}:
        for seed in seeds["cold_seeds"]:
            for repeat in range(cold_repeats):
                trials.append(
                    InstanceTrial(
                        candidates=candidates,
                        seed=seed,
                        repeat=repeat,
                        size=52,
                        density=None,
                        timeout_seconds=timeout_seconds,
                    )
                )
    if mode in {"shift", "all"}:
        for size in SHIFT_SIZES:
            for density in SHIFT_DENSITIES:
                for seed in seeds["shift_seeds"]:
                    for repeat in range(shift_repeats):
                        trials.append(
                            InstanceTrial(
                                candidates=candidates,
                                seed=seed,
                                repeat=repeat,
                                size=size,
                                density=density,
                                timeout_seconds=timeout_seconds,
                            )
                        )
    return trials


def _aggregate(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, float | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["arm"]), int(row["size"]), row["density"])].append(row)
    summaries: list[dict[str, Any]] = []
    for (arm, size, density), items in sorted(
        grouped.items(), key=lambda item: (item[0][1], -1.0 if item[0][2] is None else item[0][2], item[0][0])
    ):
        statuses: dict[str, int] = defaultdict(int)
        for item in items:
            statuses[str(item["status"])] += 1
        passed = [item for item in items if item["status"] == "PASS"]
        correct = len(passed) == len(items)
        by_seed: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in passed:
            by_seed[int(item["seed"])].append(item)
        candidate_ns = sum(
            min(int(item["candidate_ns"]) for item in seed_items)
            for seed_items in by_seed.values()
        )
        reference_ns = sum(
            min(int(item["reference_ns"]) for item in seed_items)
            for seed_items in by_seed.values()
        )
        summaries.append(
            {
                "arm": arm,
                "size": size,
                "density": density,
                "trial_count": len(items),
                "seed_count": len({int(item["seed"]) for item in items}),
                "status_counts": dict(sorted(statuses.items())),
                "valid_rate": len(passed) / len(items) if items else 0.0,
                "correct": correct,
                "candidate_time_ns": candidate_ns,
                "reference_time_ns": reference_ns,
                "raw_speedup": (
                    reference_ns / candidate_ns
                    if correct and candidate_ns > 0 and reference_ns > 0
                    else 0.0
                ),
            }
        )
    return summaries


def _candidate_solve_count(rows: Iterable[dict[str, Any]]) -> int:
    return sum(
        not str(row["status"]).startswith(("REFERENCE_", "INSTANCE_PROCESS_"))
        for row in rows
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    source_run = args.source_run.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = _candidate_lock(source_run, run_dir)
    seeds = _fresh_seeds(
        run_dir=run_dir,
        source_run=source_run,
        cold_count=args.cold_count,
        shift_count=args.shift_count,
    )
    trials = _trials(
        lock=lock,
        seeds=seeds,
        mode=args.mode,
        cold_repeats=args.cold_repeats,
        shift_repeats=args.shift_repeats,
        timeout_seconds=args.timeout_seconds,
    )
    worker_count = min(args.workers, len(os.sched_getaffinity(0)), len(trials))
    started = time.perf_counter()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    rows = _run_trials_parallel(trials, worker_count)
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    elapsed = time.perf_counter() - started
    candidate_solve_count = _candidate_solve_count(rows)
    child_cpu_seconds = (
        child_after.ru_utime
        + child_after.ru_stime
        - child_before.ru_utime
        - child_before.ru_stime
    )
    rows.sort(
        key=lambda item: (
            item["size"],
            -1.0 if item["density"] is None else item["density"],
            item["arm"],
            item["seed"],
            item["repeat"],
        )
    )
    _write_json(run_dir / "trials.json", rows)
    result = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "candidate_lock_sha256": _sha256_file(run_dir / "candidate_lock.json"),
        "seed_receipt_sha256": _sha256_file(run_dir / "seed_receipt.json"),
        "cold_repeats": args.cold_repeats,
        "shift_repeats": args.shift_repeats,
        "timeout_seconds": args.timeout_seconds,
        "workers": worker_count,
        "instance_process_count": len(trials),
        "candidate_trial_row_count": len(rows),
        "candidate_solve_count": candidate_solve_count,
        "wall_seconds": elapsed,
        "throughput_candidate_solves_per_second": candidate_solve_count / elapsed,
        "child_cpu_seconds": child_cpu_seconds,
        "mean_active_cpu_cores": child_cpu_seconds / elapsed,
        "allocated_cpu_utilization": child_cpu_seconds / (elapsed * worker_count),
        "fresh_process_per_instance_repeat": True,
        "fresh_process_per_solver_call": True,
        "one_solve_per_candidate_per_process": True,
        "summaries": _aggregate(rows),
        "claim_scope": "FALSIFICATION_ONLY",
        "superiority_claim_permitted": False,
    }
    _write_json(run_dir / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Cold-start and shift falsification")
    parser.add_argument(
        "--source-run",
        type=Path,
        default=REPO_ROOT / "runs" / "algotune_set_cover_blind_v4_corrected",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=REPO_ROOT / "runs" / "algotune_set_cover_falsification_v0",
    )
    parser.add_argument("--mode", choices=("cold", "shift", "all"), default="all")
    parser.add_argument("--cold-count", type=int, default=100)
    parser.add_argument("--cold-repeats", type=int, default=10)
    parser.add_argument("--shift-count", type=int, default=20)
    parser.add_argument("--shift-repeats", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=18)
    args = parser.parse_args()
    if min(
        args.cold_count,
        args.cold_repeats,
        args.shift_count,
        args.shift_repeats,
        args.workers,
    ) < 1:
        parser.error("counts, repeats, and workers must be positive")
    print(json.dumps(run(args), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
