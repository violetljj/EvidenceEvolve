from __future__ import annotations

import argparse
import json
import os
import resource
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.benchmarks import algotune_falsification as cold_set_cover
from evidence_evolve.benchmarks import algotune_heterogeneous as heterogeneous
from evidence_evolve.benchmarks.algotune_horizon_scaling import (
    ARMS,
    HORIZONS,
    PROTOCOL,
    REPO_ROOT,
    TASKS,
)
from evidence_evolve.benchmarks.algotune_official import (
    evaluate_official_candidate_cold,
)
from evidence_evolve.hashing import sha256_file


LOCK_NAME = "checkpoint_candidate_lock.json"
SEED_NAME = "heldout_seeds.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _slots(root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    tasks: dict[str, dict[str, dict[str, Any]]] = {}
    for task_name in TASKS:
        task_slots: dict[str, dict[str, Any]] = {}
        for arm in ARMS:
            trajectory_path = root / task_name / "arms" / arm / "trajectory_result.json"
            trajectory = _read_json(trajectory_path)
            if trajectory.get("task") != task_name or trajectory.get("arm") != arm:
                raise ValueError(f"trajectory identity drift: {task_name}:{arm}")
            checkpoints = {
                int(item["horizon"]): item for item in trajectory["checkpoints"]
            }
            if tuple(sorted(checkpoints)) != HORIZONS:
                raise ValueError(f"checkpoint horizons drift: {task_name}:{arm}")
            for horizon in HORIZONS:
                item = checkpoints[horizon]
                path = Path(item["candidate_path"]).resolve()
                digest = sha256_file(path)
                if digest != item["candidate_sha256"]:
                    raise ValueError(
                        f"checkpoint candidate drift: {task_name}:{arm}:h{horizon}"
                    )
                task_slots[f"{arm}@h{horizon:03d}"] = {
                    "arm": arm,
                    "horizon": horizon,
                    "candidate_path": str(path),
                    "candidate_sha256": digest,
                    "selected_id": item["selected_id"],
                    "selected_generation": int(item["selected_generation"]),
                    "development_raw_speedup": float(
                        item["development_raw_speedup"]
                    ),
                    "cumulative_tokens": int(item["cumulative_tokens"]),
                    "cumulative_wall_seconds": float(
                        item["cumulative_wall_seconds"]
                    ),
                    "proposal_valid_rate": float(item["proposal_valid_rate"]),
                }
        tasks[task_name] = task_slots
    return tasks


def lock_checkpoint_portfolio(root: Path) -> dict[str, Any]:
    root = root.resolve()
    lock_path = root / LOCK_NAME
    tasks = _slots(root)
    if lock_path.exists():
        existing = _read_json(lock_path)
        if existing.get("tasks") != tasks:
            raise ValueError("checkpoint portfolio drift after lock")
        return existing
    seed_receipts = list(root.glob(f"*/{SEED_NAME}"))
    if seed_receipts:
        raise ValueError("held-out seeds existed before all checkpoints were locked")
    payload = {
        "schema_version": "1.0",
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL),
        "all_checkpoint_slots_locked": True,
        "checkpoint_slot_count": len(TASKS) * len(ARMS) * len(HORIZONS),
        "heldout_existed_at_lock": False,
        "tasks": tasks,
    }
    create_once_json(lock_path, payload)
    return payload


def _fresh_seeds(root: Path, task_name: str) -> list[int]:
    lock_path = root / LOCK_NAME
    if not lock_path.exists():
        raise ValueError("checkpoint portfolio must be locked before held-out seeds")
    path = root / task_name / SEED_NAME
    if path.exists():
        return [int(seed) for seed in _read_json(path)["seeds"]]
    count = 100
    excluded = set(range(100 if task_name == "set_cover" else 20))
    values: set[int] = set()
    while len(values) < count:
        value = (
            secrets.randbits(63)
            if task_name == "set_cover"
            else secrets.randbelow(2**32)
        )
        if value not in excluded:
            values.add(value)
    create_once_json(
        path,
        {
            "schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_after_all_checkpoint_slots_locked": True,
            "checkpoint_candidate_lock_sha256": sha256_file(lock_path),
            "generator": "Python secrets.SystemRandom / OS CSPRNG",
            "task_seed_domain": "uint63" if task_name == "set_cover" else "uint32",
            "development_seeds_excluded": True,
            "seeds": sorted(values),
        },
    )
    return sorted(values)


def _deduplicate(
    task_slots: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    unique: dict[str, dict[str, Any]] = {}
    slot_to_representative: dict[str, str] = {}
    for slot, item in sorted(task_slots.items()):
        digest = str(item["candidate_sha256"])
        representative = f"candidate-{digest}"
        unique.setdefault(representative, item)
        slot_to_representative[slot] = representative
    return unique, slot_to_representative


def _scientific_outcome(heldout: dict[str, Any]) -> str:
    statuses = set(heldout.get("status_counts", {}))
    shared_truth_failure = any(
        status.startswith(("REFERENCE_", "INSTANCE_PROCESS_"))
        or status
        in {
            "ERROR_SETUP",
            "ERROR_REFERENCE",
            "TIMEOUT_SETUP",
            "TIMEOUT_REFERENCE",
            "TIMEOUT_VERIFY",
        }
        for status in statuses
    )
    if shared_truth_failure:
        return "NOT_EVALUABLE_DATA"
    if not heldout.get("correct"):
        return "INVALID_MECHANICS_OR_ADAPTER"
    return (
        "POSITIVE_HEADROOM"
        if float(heldout.get("raw_speedup", 0.0)) > 1.0
        else "VALID_NEGATIVE"
    )


def _evaluate_set_cover(
    task_dir: Path,
    unique: dict[str, dict[str, Any]],
    seeds: list[int],
) -> dict[str, dict[str, Any]]:
    receipt_path = task_dir / "unique_heldout_results.json"
    if receipt_path.exists():
        receipt = _read_json(receipt_path)
        expected_hashes = sorted(item["candidate_sha256"] for item in unique.values())
        if receipt.get("candidate_sha256s") != expected_hashes:
            raise ValueError("set-cover unique held-out candidate set drift")
        if receipt.get("heldout_seed_receipt_sha256") != sha256_file(
            task_dir / SEED_NAME
        ):
            raise ValueError("set-cover held-out seed receipt drift")
        return receipt["results"]
    candidates = tuple(
        (name, str(item["candidate_path"])) for name, item in sorted(unique.items())
    )
    trials = [
        cold_set_cover.InstanceTrial(
            candidates=candidates,
            seed=seed,
            repeat=repeat,
            size=52,
            density=None,
            timeout_seconds=10.0,
        )
        for seed in seeds
        for repeat in range(10)
    ]
    workers = min(18, len(os.sched_getaffinity(0)), len(trials))
    started = time.perf_counter()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    rows = cold_set_cover._run_trials_parallel(trials, workers)
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall_seconds = time.perf_counter() - started
    summaries = {
        str(item["arm"]): item for item in cold_set_cover._aggregate(rows)
    }
    child_cpu_seconds = (
        child_after.ru_utime
        + child_after.ru_stime
        - child_before.ru_utime
        - child_before.ru_stime
    )
    payload = {
        "schema_version": "1.0",
        "task": "set_cover",
        "candidate_sha256s": sorted(
            item["candidate_sha256"] for item in unique.values()
        ),
        "heldout_seed_receipt_sha256": sha256_file(task_dir / SEED_NAME),
        "results": summaries,
        "unique_candidate_count": len(unique),
        "trial_count": len(trials),
        "candidate_solve_count": cold_set_cover._candidate_solve_count(rows),
        "workers": workers,
        "wall_seconds": wall_seconds,
        "child_cpu_seconds": child_cpu_seconds,
        "mean_active_cpu_cores": child_cpu_seconds / wall_seconds,
        "fresh_process_per_timed_candidate_call": True,
    }
    create_once_json(receipt_path, payload)
    return summaries


def _evaluate_heterogeneous(
    task_dir: Path,
    task_name: str,
    unique: dict[str, dict[str, Any]],
    seeds: list[int],
) -> dict[str, dict[str, Any]]:
    task = heterogeneous._task_payload(task_name)
    spec = heterogeneous._spec(task)
    receipt_dir = task_dir / "unique_heldout_results"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    workers = heterogeneous.HELDOUT_WORKERS.get(task_name, 18)
    for name, item in sorted(unique.items()):
        receipt_path = receipt_dir / f"{item['candidate_sha256']}.json"
        if receipt_path.exists():
            receipt = _read_json(receipt_path)
        else:
            heldout = evaluate_official_candidate_cold(
                item["candidate_path"],
                spec,
                seeds,
                repeats=10,
                workers=workers,
                timeout_seconds=60.0,
            )
            receipt = {
                "schema_version": "1.0",
                "task": task_name,
                "candidate_sha256": item["candidate_sha256"],
                "heldout_seed_receipt_sha256": sha256_file(task_dir / SEED_NAME),
                "workers": workers,
                "fresh_process_per_timed_candidate_call": True,
                "heldout": heldout,
            }
            create_once_json(receipt_path, receipt)
        if receipt["candidate_sha256"] != item["candidate_sha256"]:
            raise ValueError(f"held-out receipt drift: {task_name}:{name}")
        if receipt.get("heldout_seed_receipt_sha256") != sha256_file(
            task_dir / SEED_NAME
        ):
            raise ValueError(f"held-out seed receipt drift: {task_name}:{name}")
        results[name] = receipt["heldout"]
    return results


def finalize_task(root: Path, task_name: str) -> dict[str, Any]:
    root = root.resolve()
    lock = lock_checkpoint_portfolio(root)
    task_dir = root / task_name
    result_path = task_dir / "scaling_result.json"
    if result_path.exists():
        return _read_json(result_path)
    seeds = _fresh_seeds(root, task_name)
    task_slots = lock["tasks"][task_name]
    unique, slot_to_representative = _deduplicate(task_slots)
    unique_results = (
        _evaluate_set_cover(task_dir, unique, seeds)
        if task_name == "set_cover"
        else _evaluate_heterogeneous(task_dir, task_name, unique, seeds)
    )
    results: list[dict[str, Any]] = []
    for slot, item in sorted(
        task_slots.items(), key=lambda pair: (pair[1]["arm"], pair[1]["horizon"])
    ):
        representative = slot_to_representative[slot]
        heldout = unique_results[representative]
        results.append(
            {
                "slot": slot,
                **item,
                "heldout": heldout,
                "scientific_outcome": _scientific_outcome(heldout),
                "deduplicated_representative": representative,
            }
        )
    payload = {
        "schema_version": "1.0",
        "task": task_name,
        "protocol_sha256": sha256_file(PROTOCOL),
        "checkpoint_candidate_lock_sha256": sha256_file(root / LOCK_NAME),
        "heldout_seed_receipt_sha256": sha256_file(task_dir / SEED_NAME),
        "checkpoint_slot_count": len(results),
        "unique_candidate_count": len(unique),
        "results": results,
        "claim_scope": "SINGLE_TRAJECTORY_HORIZON_SCALING_ON_EXISTING_TASK",
        "cross_engine_superiority_claim_permitted": False,
        "between_run_variance_claim_permitted": False,
        "mechanism_claim_permitted": False,
    }
    create_once_json(result_path, payload)
    return payload


def _classify_curve(points: list[dict[str, Any]]) -> str:
    if any(
        item["scientific_outcome"]
        in {"NOT_EVALUABLE_DATA", "INVALID_MECHANICS_OR_ADAPTER"}
        for item in points
    ):
        return "INVALID_OR_NOT_EVALUABLE"
    scores = [float(item["heldout"]["raw_speedup"]) for item in points]
    hashes = [str(item["candidate_sha256"]) for item in points]
    if len(set(hashes[1:])) == 1:
        return "EARLY_MATURE_BY_H6"
    late_ratios = [
        scores[index] / scores[index - 1] if scores[index - 1] > 0 else 0.0
        for index in range(2, len(scores))
    ]
    material_late_gains = sum(ratio >= 1.05 for ratio in late_ratios)
    if material_late_gains >= 2 and scores[-1] >= 1.15 * scores[1]:
        return "SUSTAINED_IMPROVEMENT"
    positive_late_gains = [
        max(scores[index] - scores[index - 1], 0.0)
        for index in range(2, len(scores))
    ]
    total_positive = sum(positive_late_gains)
    if (
        any(ratio >= 1.25 for ratio in late_ratios)
        and total_positive > 0
        and max(positive_late_gains) >= 0.60 * total_positive
    ):
        return "PUNCTUATED_BREAKTHROUGH_SIGNAL"
    if scores[-1] < 0.85 * max(scores):
        return "LATE_REGRESSION_OR_DEV_OVERFIT"
    positive_scores = [score for score in scores if score > 0]
    if positive_scores and max(positive_scores) <= 1.10 * min(positive_scores):
        return "FLAT_NO_MATERIAL_IMPROVEMENT"
    return "MIXED_TRAJECTORY"


def build_summary(root: Path) -> dict[str, Any]:
    root = root.resolve()
    curves: list[dict[str, Any]] = []
    pattern_counts: dict[str, dict[str, int]] = {arm: {} for arm in ARMS}
    for task_name in TASKS:
        result = _read_json(root / task_name / "scaling_result.json")
        for arm in ARMS:
            points = [item for item in result["results"] if item["arm"] == arm]
            points.sort(key=lambda item: int(item["horizon"]))
            pattern = _classify_curve(points)
            pattern_counts[arm][pattern] = pattern_counts[arm].get(pattern, 0) + 1
            curves.append(
                {
                    "task": task_name,
                    "arm": arm,
                    "pattern": pattern,
                    "points": [
                        {
                            "horizon": item["horizon"],
                            "heldout_raw_speedup": item["heldout"]["raw_speedup"],
                            "correct": item["heldout"]["correct"],
                            "candidate_sha256": item["candidate_sha256"],
                            "cumulative_tokens": item["cumulative_tokens"],
                            "cumulative_wall_seconds": item[
                                "cumulative_wall_seconds"
                            ],
                            "proposal_valid_rate": item["proposal_valid_rate"],
                        }
                        for item in points
                    ],
                }
            )
    payload = {
        "schema_version": "1.0",
        "protocol_sha256": sha256_file(PROTOCOL),
        "checkpoint_candidate_lock_sha256": sha256_file(root / LOCK_NAME),
        "curves": curves,
        "exploratory_pattern_counts": pattern_counts,
        "classification_rules": {
            "EARLY_MATURE_BY_H6": "one unchanged selected candidate from h6 through h50",
            "SUSTAINED_IMPROVEMENT": "at least two post-h6 interval gains of 5% and h50 at least 15% above h6",
            "PUNCTUATED_BREAKTHROUGH_SIGNAL": "one post-h6 jump of at least 25% contributes at least 60% of positive late gains",
            "LATE_REGRESSION_OR_DEV_OVERFIT": "h50 held-out score is more than 15% below the trajectory maximum",
            "FLAT_NO_MATERIAL_IMPROVEMENT": "maximum versus minimum valid held-out score differs by no more than 10%",
            "MIXED_TRAJECTORY": "none of the other descriptive patterns applies",
        },
        "classification_scope": "EXPLORATORY_SINGLE_TRAJECTORY_DESCRIPTION_ONLY",
        "high_variance_claim_permitted": False,
        "cross_engine_superiority_claim_permitted": False,
    }
    path = root / "scaling_summary.json"
    if path.exists():
        existing = _read_json(path)
        if existing != payload:
            raise ValueError("scaling summary drift")
    else:
        create_once_json(path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Lock and replay horizon checkpoints")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT / "runs/algotune_horizon_scaling_v0",
    )
    parser.add_argument("--task", choices=TASKS)
    parser.add_argument("--lock-only", action="store_true")
    args = parser.parse_args()
    lock_checkpoint_portfolio(args.root)
    if args.lock_only:
        return 0
    task_names = (args.task,) if args.task else TASKS
    for task_name in task_names:
        finalize_task(args.root, task_name)
    if args.task is None:
        build_summary(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
