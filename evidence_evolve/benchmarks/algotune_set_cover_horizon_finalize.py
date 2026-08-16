from __future__ import annotations

import argparse
import json
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.benchmarks import algotune_falsification as cold_set_cover
from evidence_evolve.benchmarks.algotune_horizon_finalize import (
    LOCK_NAME,
    SEED_NAME,
    _classify_curve,
    _deduplicate,
    _evaluate_set_cover,
    _fresh_seeds,
    _scientific_outcome,
)
from evidence_evolve.benchmarks.algotune_horizon_scaling import (
    ARMS,
    HORIZONS,
    PROTOCOL,
    REPO_ROOT,
)
from evidence_evolve.hashing import sha256_file


TASK = "set_cover"
DEFAULT_BUNDLE = REPO_ROOT / "research/results/algotune_set_cover_horizon_scaling_v0"
DEFAULT_RUN = REPO_ROOT / "runs/algotune_set_cover_horizon_blind_v0"
CANARY_NAME = "mechanics_canary.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _bundle_slots(bundle: Path) -> dict[str, dict[str, Any]]:
    bundle = bundle.resolve()
    exported = _read_json(bundle / "result.json")
    if exported.get("protocol_sha256") != sha256_file(PROTOCOL):
        raise ValueError("exported Set Cover bundle protocol drift")
    if exported.get("heldout_seeds_generated") is not False:
        raise ValueError("exported Set Cover bundle is not development-only")
    if exported.get("heldout_evaluation_run") is not False:
        raise ValueError("exported Set Cover bundle already contains held-out evidence")
    if tuple(exported.get("horizons", ())) != HORIZONS:
        raise ValueError("exported Set Cover horizons drift")
    if set(exported.get("trajectories", {})) != set(ARMS):
        raise ValueError("exported Set Cover arms drift")

    slots: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        trajectory = exported["trajectories"][arm]
        if trajectory.get("task") != TASK or trajectory.get("arm") != arm:
            raise ValueError(f"exported trajectory identity drift: {arm}")
        checkpoints = {
            int(item["horizon"]): item for item in trajectory["checkpoints"]
        }
        if tuple(sorted(checkpoints)) != HORIZONS:
            raise ValueError(f"exported checkpoint horizons drift: {arm}")
        for horizon in HORIZONS:
            item = checkpoints[horizon]
            relative = Path(item["candidate_path"])
            if relative.is_absolute():
                raise ValueError(f"exported candidate path must be relative: {arm}:h{horizon}")
            path = (bundle / relative).resolve()
            if not path.is_relative_to(bundle):
                raise ValueError(f"exported candidate escapes bundle: {arm}:h{horizon}")
            digest = sha256_file(path)
            if digest != item["candidate_sha256"]:
                raise ValueError(f"exported candidate drift: {arm}:h{horizon}")
            slots[f"{arm}@h{horizon:03d}"] = {
                "arm": arm,
                "horizon": horizon,
                "candidate_path": str(path),
                "candidate_sha256": digest,
                "selected_id": item["selected_id"],
                "selected_generation": int(item["selected_generation"]),
                "development_raw_speedup": float(item["development_raw_speedup"]),
                "cumulative_tokens": int(item["cumulative_tokens"]),
                "cumulative_wall_seconds": float(item["cumulative_wall_seconds"]),
                "proposal_valid_rate": float(item["proposal_valid_rate"]),
            }
    if len(slots) != len(ARMS) * len(HORIZONS):
        raise ValueError("exported Set Cover checkpoint slot count drift")
    return slots


def lock_bundle(bundle: Path, run_dir: Path) -> dict[str, Any]:
    bundle = bundle.resolve()
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    slots = _bundle_slots(bundle)
    lock_path = run_dir / LOCK_NAME
    stable = {
        "protocol_sha256": sha256_file(PROTOCOL),
        "source_bundle": str(bundle),
        "source_bundle_result_sha256": sha256_file(bundle / "result.json"),
        "all_checkpoint_slots_locked": True,
        "checkpoint_slot_count": len(slots),
        "heldout_existed_at_lock": False,
        "tasks": {TASK: slots},
    }
    if lock_path.exists():
        existing = _read_json(lock_path)
        if any(existing.get(key) != value for key, value in stable.items()):
            raise ValueError("Set Cover checkpoint portfolio drift after lock")
        return existing
    if (run_dir / TASK / SEED_NAME).exists():
        raise ValueError("held-out seeds existed before Set Cover checkpoints were locked")
    payload = {
        "schema_version": "1.0",
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "SET_COVER_ONLY_FROM_FROZEN_EXPORTED_TRAJECTORIES",
        **stable,
    }
    create_once_json(lock_path, payload)
    return payload


def run_mechanics_canary(bundle: Path, run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    lock = lock_bundle(bundle, run_dir)
    if (run_dir / TASK / SEED_NAME).exists():
        raise ValueError("mechanics canary must run before held-out seed generation")
    receipt_path = run_dir / CANARY_NAME
    if receipt_path.exists():
        receipt = _read_json(receipt_path)
        if receipt.get("checkpoint_candidate_lock_sha256") != sha256_file(
            run_dir / LOCK_NAME
        ):
            raise ValueError("mechanics canary candidate lock drift")
        return receipt

    unique, _mapping = _deduplicate(lock["tasks"][TASK])
    candidates = tuple(
        (name, str(item["candidate_path"])) for name, item in sorted(unique.items())
    )
    trials = [
        cold_set_cover.InstanceTrial(
            candidates=candidates,
            seed=seed,
            repeat=0,
            size=52,
            density=None,
            timeout_seconds=10.0,
        )
        for seed in range(4)
    ]
    workers = min(4, len(os.sched_getaffinity(0)), len(trials))
    started = time.perf_counter()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    rows = cold_set_cover._run_trials_parallel(trials, workers)
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall_seconds = time.perf_counter() - started
    summaries = cold_set_cover._aggregate(rows)
    passed = bool(summaries) and all(item["correct"] for item in summaries)
    payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "checkpoint_candidate_lock_sha256": sha256_file(run_dir / LOCK_NAME),
        "purpose": "PARALLEL_MECHANICS_ADMISSION_ONLY",
        "scientific_evidence": False,
        "development_seeds": list(range(4)),
        "heldout_seeds_generated": False,
        "unique_candidate_count": len(unique),
        "workers": workers,
        "wall_seconds": wall_seconds,
        "child_cpu_seconds": (
            child_after.ru_utime
            + child_after.ru_stime
            - child_before.ru_utime
            - child_before.ru_stime
        ),
        "status": "PASS" if passed else "FAIL",
        "summaries": summaries,
    }
    create_once_json(receipt_path, payload)
    if not passed:
        raise RuntimeError("Set Cover mechanics canary failed; held-out remains unopened")
    return payload


def finalize(bundle: Path, run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    lock = lock_bundle(bundle, run_dir)
    canary_path = run_dir / CANARY_NAME
    if not canary_path.exists():
        raise ValueError("passing mechanics canary required before held-out evaluation")
    canary = _read_json(canary_path)
    expected_lock_sha256 = sha256_file(run_dir / LOCK_NAME)
    if (
        canary.get("status") != "PASS"
        or canary.get("checkpoint_candidate_lock_sha256") != expected_lock_sha256
        or canary.get("heldout_seeds_generated") is not False
        or canary.get("scientific_evidence") is not False
    ):
        raise ValueError("valid mechanics canary receipt required before held-out evaluation")
    result_path = run_dir / TASK / "scaling_result.json"
    if result_path.exists():
        return _read_json(result_path)

    seeds = _fresh_seeds(run_dir, TASK)
    task_slots = lock["tasks"][TASK]
    unique, slot_to_representative = _deduplicate(task_slots)
    unique_results = _evaluate_set_cover(run_dir / TASK, unique, seeds)
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
    curves = []
    for arm in ARMS:
        points = sorted(
            (item for item in results if item["arm"] == arm),
            key=lambda item: int(item["horizon"]),
        )
        curves.append(
            {
                "arm": arm,
                "pattern": _classify_curve(points),
                "points": [
                    {
                        "horizon": item["horizon"],
                        "heldout_raw_speedup": item["heldout"]["raw_speedup"],
                        "scientific_outcome": item["scientific_outcome"],
                        "candidate_sha256": item["candidate_sha256"],
                        "cumulative_tokens": item["cumulative_tokens"],
                        "cumulative_wall_seconds": item["cumulative_wall_seconds"],
                    }
                    for item in points
                ],
            }
        )
    payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task": TASK,
        "protocol_sha256": sha256_file(PROTOCOL),
        "checkpoint_candidate_lock_sha256": sha256_file(run_dir / LOCK_NAME),
        "heldout_seed_receipt_sha256": sha256_file(run_dir / TASK / SEED_NAME),
        "mechanics_canary_sha256": sha256_file(canary_path),
        "checkpoint_slot_count": len(results),
        "unique_candidate_count": len(unique),
        "results": results,
        "curves": curves,
        "claim_scope": "SINGLE_TRAJECTORY_SET_COVER_HORIZON_SCALING",
        "cross_engine_superiority_claim_permitted": False,
        "between_run_variance_claim_permitted": False,
        "mechanism_claim_permitted": False,
    }
    create_once_json(result_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Blind replay of the frozen Set Cover horizon-scaling bundle"
    )
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--lock-only", action="store_true")
    mode.add_argument("--canary-only", action="store_true")
    args = parser.parse_args()
    if args.lock_only:
        result = lock_bundle(args.bundle, args.run_dir)
    elif args.canary_only:
        result = run_mechanics_canary(args.bundle, args.run_dir)
    else:
        result = finalize(args.bundle, args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
