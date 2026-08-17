from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.benchmarks import algotune_blind as blind
from evidence_evolve.benchmarks import algotune_heterogeneous as heterogeneous
from evidence_evolve.benchmarks import algotune_horizon_scaling as scaling
from evidence_evolve.benchmarks.algotune_official import (
    OfficialTaskSpec,
    evaluate_official_candidate,
    evaluate_official_candidate_cold,
)
from evidence_evolve.hashing import sha256_bytes, sha256_file, sha256_object
from evidence_evolve.remote_cpu import (
    RemoteEntrypoint,
    create_job_request,
    dispatch_job,
    verify_result,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "research/parity/m4_search_value_tournament_v0.protocol.json"
UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_m4_upstream"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs/m4_search_value_tournament_v0"
ARMS = ("vanilla", "shinka", "ada", "evidence_evolve")
HORIZONS = (1, 2, 3)
REMOTE_HOST = "root@connect.westb.seetacloud.com"
REMOTE_PORT = 16288
REMOTE_ROOT = "/root/autodl-tmp/evidence-evolve-worker"


def _load_protocol() -> dict[str, Any]:
    payload = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if tuple(payload["arms"]) != ARMS:
        raise ValueError("M4 arm set drift")
    if tuple(payload["repeats"]) != (1, 2, 3):
        raise ValueError("M4 repeat set drift")
    for task in payload["tasks"]:
        source = _source_path(str(task["task"]))
        if sha256_file(source) != task["source_sha256"]:
            raise ValueError(f"M4 upstream source drift: {task['task']}")
    checkpoint = str(payload["ee_policy_checkpoint"])
    policy = REPO_ROOT / "research/policies/algotune_set_cover_blind_v0.yaml"
    frozen_policy = subprocess.run(
        ["git", "show", f"{checkpoint}:{policy.relative_to(REPO_ROOT).as_posix()}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    current_policy = policy.read_bytes().replace(b"\r\n", b"\n")
    if sha256_bytes(frozen_policy.replace(b"\r\n", b"\n")) != sha256_bytes(
        current_policy
    ):
        raise ValueError("current EE control policy differs from fd53fba")
    minimum = str(payload["execution_layer_minimum_commit"])
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", minimum, "HEAD"],
        cwd=REPO_ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError("M4 execution layer is older than the admitted minimum")
    return payload


def _source_path(task_name: str) -> Path:
    return UPSTREAM_ROOT / "AlgoTuneTasks" / task_name / f"{task_name}.py"


def _task_payload(task_name: str) -> dict[str, Any]:
    for item in _load_protocol()["tasks"]:
        if item["task"] == task_name:
            return item
    raise ValueError(f"unknown M4 task: {task_name}")


def _task_spec(task: dict[str, Any]) -> OfficialTaskSpec:
    return OfficialTaskSpec(
        name=str(task["task"]),
        class_name=str(task["class"]),
        problem_size=int(task["problem_size"]),
        source_path=str(_source_path(str(task["task"]))),
    )


def _conditions() -> dict[str, Any]:
    return _load_protocol()["common_conditions"]


def run_remote_evaluator(
    *,
    task_name: str,
    candidate: Path,
    seeds_path: Path,
    repeats: int,
    workers: int,
    cold: bool,
    output: Path,
) -> dict[str, Any]:
    task = _task_payload(task_name)
    spec = _task_spec(task)
    seeds = [int(value) for value in json.loads(seeds_path.read_text())["seeds"]]
    if cold:
        result = evaluate_official_candidate_cold(
            candidate,
            spec,
            seeds,
            repeats=repeats,
            workers=workers,
            timeout_seconds=60.0,
        )
    else:
        result = evaluate_official_candidate(
            candidate, spec, seeds, repeats=repeats, workers=workers
        )
    payload = {
        **result,
        "task": task_name,
        "candidate_sha256": sha256_file(candidate),
        "seeds_sha256": sha256_file(seeds_path),
        "execution_authority": "REMOTE_EXECUTION_ONLY",
    }
    blind._write_json(output, payload)
    return payload


def _remote_evaluate(
    candidate: Path,
    spec: OfficialTaskSpec,
    seeds: list[int],
    *,
    repeats: int,
    workers: int,
    cold: bool,
) -> dict[str, Any]:
    context = os.environ.get("EE_M4_EVAL_CONTEXT", "unscoped")
    repository_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    key = hashlib.sha256(
        json.dumps(
            {
                "repository_commit": repository_commit,
                "task": spec.name,
                "candidate": sha256_file(candidate),
                "seeds": seeds,
                "repeats": repeats,
                "workers": workers,
                "cold": cold,
                "context": context,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:32]
    job_dir = REPO_ROOT / "runs/m4_search_value_tournament_v0/remote_jobs" / key
    job_dir.mkdir(parents=True, exist_ok=True)
    staged_candidate = job_dir / "candidate.py"
    staged_seeds = job_dir / "seeds.json"
    if not staged_candidate.exists():
        shutil.copy2(candidate, staged_candidate)
    elif sha256_file(staged_candidate) != sha256_file(candidate):
        raise ValueError("remote evaluation candidate key collision")
    seeds_payload = {"seeds": seeds}
    if not staged_seeds.exists():
        blind._write_json(staged_seeds, seeds_payload)
    elif json.loads(staged_seeds.read_text()) != seeds_payload:
        raise ValueError("remote evaluation seed key collision")

    request_path = job_dir / "request.json"
    result_dir = job_dir / "result"
    output_relative = (job_dir / "output.json").relative_to(REPO_ROOT).as_posix()
    if not request_path.exists():
        create_job_request(
            repo=REPO_ROOT,
            output=request_path,
            job_id=f"m4-{key}",
            entrypoint=RemoteEntrypoint.PYTHON_MODULE,
            argv=(
                "evidence_evolve.benchmarks.search_value_m4",
                "remote-evaluate",
                "--task",
                spec.name,
                "--candidate",
                staged_candidate.relative_to(REPO_ROOT).as_posix(),
                "--seeds",
                staged_seeds.relative_to(REPO_ROOT).as_posix(),
                "--repeats",
                str(repeats),
                "--workers",
                str(workers),
                *(('--cold',) if cold else ()),
                "--output",
                output_relative,
            ),
            input_paths=(
                PROTOCOL.relative_to(REPO_ROOT).as_posix(),
                _source_path(spec.name).relative_to(REPO_ROOT).as_posix(),
                staged_candidate.relative_to(REPO_ROOT).as_posix(),
                staged_seeds.relative_to(REPO_ROOT).as_posix(),
            ),
            output_paths=(output_relative,),
            cpu_workers=workers,
            timeout_seconds=1800,
        )
    if result_dir.exists():
        receipt = verify_result(request_path, result_dir)
    else:
        receipt = dispatch_job(
            repo=REPO_ROOT,
            request_path=request_path,
            host=os.environ.get("EE_REMOTE_HOST", REMOTE_HOST),
            port=int(os.environ.get("EE_REMOTE_PORT", str(REMOTE_PORT))),
            remote_root=os.environ.get("EE_REMOTE_ROOT", REMOTE_ROOT),
            local_result_dir=result_dir,
        )
    if receipt.receipt.state != "SUCCEEDED":
        raise RuntimeError(f"remote M4 evaluation failed: {receipt.receipt.state}")
    returned = result_dir / "artifacts" / output_relative
    payload = json.loads(returned.read_text(encoding="utf-8"))
    payload["remote_receipt_sha256"] = receipt.receipt_sha256
    payload["remote_receipt_path"] = str((result_dir / "receipt.json").resolve())
    return payload


def remote_development_evaluate(
    candidate: str | Path, spec: OfficialTaskSpec, *, workers: int | None = None
) -> dict[str, Any]:
    conditions = _conditions()
    start = int(os.environ.get("EE_ALGOTUNE_DEV_START", "0"))
    count = int(os.environ.get("EE_ALGOTUNE_DEV_COUNT", "20"))
    worker_count = workers or int(conditions["evaluator_workers_per_active_run"])
    raw = _remote_evaluate(
        Path(candidate),
        spec,
        list(range(start, start + count)),
        repeats=int(os.environ.get("EE_ALGOTUNE_DEV_REPEATS", "2")),
        workers=worker_count,
        cold=False,
    )
    return {
        "mechanics_status": "PASS",
        "metrics": {
            "invalid_solution_rate": 1.0 - float(raw["valid_rate"]),
            "raw_speedup": float(raw["raw_speedup"]),
        },
        "controls": {
            "candidate_valid": bool(raw["correct"]),
            "development_only": True,
        },
        "error": str(raw.get("failure", "")),
        "remote_receipt_sha256": raw["remote_receipt_sha256"],
    }


def _configure(run_dir: Path, task_name: str, repeat: int) -> dict[str, Any]:
    protocol = _load_protocol()
    task = _task_payload(task_name)
    conditions = protocol["common_conditions"]
    seed_count = int(conditions["development_seeds_per_repeat"])
    os.environ.update(
        {
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "EE_ALGOTUNE_DEV_START": str((repeat - 1) * seed_count),
            "EE_ALGOTUNE_DEV_COUNT": str(seed_count),
            "EE_ALGOTUNE_DEV_REPEATS": str(conditions["development_repeats"]),
            "EE_ALGOTUNE_WORKERS": str(conditions["evaluator_workers_per_active_run"]),
            "EE_HETERO_GENERATIONS": str(conditions["proposal_calls"]),
            "EE_M4_REMOTE_EVALUATOR": "1",
        }
    )
    heterogeneous.PROTOCOL = PROTOCOL
    heterogeneous.UPSTREAM_ROOT = UPSTREAM_ROOT
    heterogeneous._load_protocol = lambda: protocol
    heterogeneous._development = remote_development_evaluate
    heterogeneous._configure(run_dir, task)
    blind.MODEL = str(conditions["model"])
    blind.REASONING_EFFORT = str(conditions["reasoning_effort"])
    blind.GENERATIONS = int(conditions["proposal_calls"])
    blind.DEV_COUNT = seed_count
    blind.DEV_REPEATS = int(conditions["development_repeats"])
    blind.TOKEN_CEILING = int(conditions["observed_token_ceiling"])
    blind.WALL_CEILING_SECONDS = float(conditions["wall_seconds"])
    blind.EVALUATOR_WORKERS = int(conditions["evaluator_workers_per_active_run"])
    scaling.HORIZONS = HORIZONS
    return task


def _create_manifest(run_dir: Path, task_name: str, repeat: int) -> None:
    protocol = _load_protocol()
    payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL),
        "task": task_name,
        "repeat": repeat,
        "arms": list(ARMS),
        "ee_policy_checkpoint": protocol["ee_policy_checkpoint"],
        "conditions": protocol["common_conditions"],
        "claim_scope": protocol["claim_scope"],
    }
    path = run_dir / "m4_manifest.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "protocol_sha256",
            "task",
            "repeat",
            "arms",
            "ee_policy_checkpoint",
            "conditions",
            "claim_scope",
        ):
            if existing.get(key) != payload[key]:
                raise ValueError(f"M4 manifest drift: {key}")
        return
    create_once_json(path, payload)


def _extract_vanilla(
    arm_dir: Path, arm_result: dict[str, Any], started_epoch: float
) -> list[dict[str, Any]]:
    history = json.loads((arm_dir / "history.json").read_text(encoding="utf-8"))
    seed = arm_dir / "candidates/seed.py"
    candidates: list[dict[str, Any]] = [
        {"generation": 0, "score": 1.0, "path": seed, "id": "SEED"}
    ]
    for row in history:
        generation = int(row["iteration"])
        metrics = row["metrics"]
        if metrics["controls"].get("candidate_valid"):
            candidates.append(
                {
                    "generation": generation,
                    "score": float(metrics["metrics"]["raw_speedup"]),
                    "path": arm_dir / "candidates" / f"candidate_{generation:03d}.py",
                    "id": f"VAN-{generation:03d}",
                }
            )
    records: list[dict[str, Any]] = []
    event_paths = sorted((arm_dir / "calls").glob("*.events.jsonl"))
    for horizon in HORIZONS:
        selected = max(
            (item for item in candidates if item["generation"] <= horizon),
            key=lambda item: item["score"],
        )
        eligible_events = event_paths[:horizon]
        wall = max(
            (
                path.stat().st_mtime
                for path in [*eligible_events, Path(selected["path"])]
                if path.exists()
            ),
            default=started_epoch,
        ) - started_epoch
        records.append(
            scaling._copy_checkpoint(
                arm_dir,
                horizon,
                Path(selected["path"]).read_text(encoding="utf-8"),
                selected_id=str(selected["id"]),
                selected_generation=int(selected["generation"]),
                dev_score=float(selected["score"]),
                tokens=(
                    int(arm_result["tokens"])
                    if horizon == HORIZONS[-1]
                    else scaling._events_usage(eligible_events)
                ),
                wall_seconds=(
                    float(arm_result["wall_seconds"])
                    if horizon == HORIZONS[-1]
                    else max(wall, 0.0)
                ),
                proposal_valid_rate=(
                    sum(1 for item in candidates if 0 < item["generation"] <= horizon)
                    / horizon
                ),
            )
        )
    return records


def run_arm(run_root: Path, task_name: str, repeat: int, arm: str) -> dict[str, Any]:
    if arm not in ARMS:
        raise ValueError(f"unsupported M4 arm: {arm}")
    os.environ["EE_M4_EVAL_CONTEXT"] = f"{task_name}-r{repeat}-{arm}"
    run_dir = run_root / task_name / f"repeat_{repeat:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    task = _configure(run_dir, task_name, repeat)
    _create_manifest(run_dir, task_name, repeat)
    arm_dir = run_dir / "arms" / arm
    trajectory_path = arm_dir / "trajectory_result.json"
    if trajectory_path.exists():
        return json.loads(trajectory_path.read_text(encoding="utf-8"))
    started_epoch = time.time()
    if arm == "evidence_evolve":
        arm_result = heterogeneous._run_evidence(run_dir, task, _task_spec(task))
        checkpoints = scaling._extract_evidence(
            run_dir, task_name, arm_result, started_epoch
        )
    elif arm == "shinka":
        arm_result = blind.run_shinka(run_dir)
        checkpoints = scaling._extract_shinka(arm_dir, arm_result, started_epoch)
    elif arm == "ada":
        arm_result = blind.run_ada(run_dir)
        checkpoints = scaling._extract_sky(arm_dir, arm_result, started_epoch)
    else:
        arm_result = blind.run_vanilla(run_dir)
        checkpoints = _extract_vanilla(arm_dir, arm_result, started_epoch)
    conditions = _conditions()
    budget_violation = bool(
        int(arm_result["tokens"]) > int(conditions["observed_token_ceiling"])
        or float(arm_result["wall_seconds"]) > float(conditions["wall_seconds"])
    )
    payload = {
        "schema_version": "1.0",
        "task": task_name,
        "repeat": repeat,
        "arm": arm,
        "model": blind.MODEL,
        "reasoning_effort": blind.REASONING_EFFORT,
        "continuous_trajectory": True,
        "budget_violation": budget_violation,
        "checkpoints": checkpoints,
        "final_arm_result": arm_result,
    }
    blind._write_json(trajectory_path, payload)
    return payload


def _trial_commands(run_root: Path) -> list[tuple[str, int, str, list[str]]]:
    commands = []
    for task in _load_protocol()["tasks"]:
        for repeat in (1, 2, 3):
            for arm in ARMS:
                commands.append(
                    (
                        str(task["task"]),
                        repeat,
                        arm,
                        [
                            sys.executable,
                            "-m",
                            "evidence_evolve.benchmarks.search_value_m4",
                            "arm",
                            "--run-root",
                            str(run_root),
                            "--task",
                            str(task["task"]),
                            "--repeat",
                            str(repeat),
                            "--arm",
                            arm,
                        ],
                    )
                )
    return commands


def _run_subprocess_trial(
    item: tuple[str, int, str, list[str]], run_root: Path
) -> dict[str, Any]:
    task, repeat, arm, command = item
    trial_dir = run_root / task / f"repeat_{repeat:02d}"
    process_dir = trial_dir / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status_path = process_dir / f"{arm}.json"
    if status_path.exists():
        return json.loads(status_path.read_text(encoding="utf-8"))
    stdout_path = process_dir / f"{arm}.stdout.log"
    stderr_path = process_dir / f"{arm}.stderr.log"
    started = time.monotonic()
    state = "FAILED"
    returncode: int | None = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                check=False,
                stdout=stdout,
                stderr=stderr,
                timeout=float(_conditions()["wall_seconds"]) + 60.0,
                env=os.environ.copy(),
            )
        returncode = completed.returncode
        state = "SUCCEEDED" if returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired:
        state = "TIMED_OUT"
    payload = {
        "task": task,
        "repeat": repeat,
        "arm": arm,
        "state": state,
        "returncode": returncode,
        "elapsed_seconds": time.monotonic() - started,
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
    }
    blind._write_json(status_path, payload)
    return payload


def run_search(run_root: Path, max_parallel: int) -> list[dict[str, Any]]:
    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(_run_subprocess_trial, item, run_root): item[:3]
            for item in _trial_commands(run_root)
        }
        for future in as_completed(futures):
            results.append(future.result())
    blind._write_json(run_root / "search_process_summary.json", results)
    return results


def lock_portfolio(run_root: Path) -> dict[str, Any]:
    if any(run_root.rglob("heldout_seeds.json")):
        raise ValueError("held-out seeds existed before the M4 portfolio lock")
    entries: list[dict[str, Any]] = []
    for task in _load_protocol()["tasks"]:
        task_name = str(task["task"])
        for repeat in (1, 2, 3):
            for arm in ARMS:
                trajectory_path = (
                    run_root
                    / task_name
                    / f"repeat_{repeat:02d}"
                    / "arms"
                    / arm
                    / "trajectory_result.json"
                )
                if not trajectory_path.exists():
                    raise ValueError(f"missing M4 trajectory: {task_name}:{repeat}:{arm}")
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                checkpoints = []
                for checkpoint in trajectory["checkpoints"]:
                    candidate = Path(checkpoint["candidate_path"])
                    if sha256_file(candidate) != checkpoint["candidate_sha256"]:
                        raise ValueError(
                            f"candidate drift: {task_name}:{repeat}:{arm}:h{checkpoint['horizon']}"
                        )
                    checkpoints.append(
                        {
                            "horizon": checkpoint["horizon"],
                            "candidate_path": str(candidate.resolve()),
                            "candidate_sha256": checkpoint["candidate_sha256"],
                        }
                    )
                entries.append(
                    {
                        "task": task_name,
                        "repeat": repeat,
                        "arm": arm,
                        "trajectory_sha256": sha256_file(trajectory_path),
                        "budget_violation": trajectory["budget_violation"],
                        "checkpoints": checkpoints,
                    }
                )
    payload = {
        "schema_version": "1.0",
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL),
        "all_36_trajectories_locked": len(entries) == 36,
        "all_108_checkpoint_candidates_locked": sum(
            len(item["checkpoints"]) for item in entries
        )
        == 108,
        "heldout_existed_at_lock": False,
        "entries": entries,
    }
    path = run_root / "portfolio_candidate_lock.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    create_once_json(path, payload)
    return payload


def _heldout_seeds(run_root: Path, task_name: str, repeat: int) -> list[int]:
    path = run_root / task_name / f"repeat_{repeat:02d}" / "heldout_seeds.json"
    if path.exists():
        return [int(value) for value in json.loads(path.read_text())["seeds"]]
    if not (run_root / "portfolio_candidate_lock.json").exists():
        raise ValueError("portfolio lock is required before held-out seed generation")
    count = int(_conditions()["heldout_seeds_per_repeat"])
    excluded = set(range(60))
    values: set[int] = set()
    while len(values) < count:
        value = secrets.randbelow(2**32)
        if value not in excluded:
            values.add(value)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_after_portfolio_lock": True,
        "portfolio_lock_sha256": sha256_file(
            run_root / "portfolio_candidate_lock.json"
        ),
        "generator": "Python secrets.SystemRandom / OS CSPRNG",
        "seeds": sorted(values),
    }
    create_once_json(path, payload)
    return payload["seeds"]


def _evaluate_block(
    run_root: Path, task_name: str, repeat: int, arm: str
) -> dict[str, Any]:
    run_dir = run_root / task_name / f"repeat_{repeat:02d}"
    result_path = run_dir / "heldout" / f"{arm}.json"
    if result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    task = _task_payload(task_name)
    spec = _task_spec(task)
    seeds = _heldout_seeds(run_root, task_name, repeat)
    trajectory = json.loads(
        (run_dir / "arms" / arm / "trajectory_result.json").read_text(
            encoding="utf-8"
        )
    )
    evaluations: list[dict[str, Any]] = []
    for checkpoint in trajectory["checkpoints"]:
        candidate = Path(checkpoint["candidate_path"])
        if sha256_file(candidate) != checkpoint["candidate_sha256"]:
            raise ValueError(f"candidate drift before heldout: {task_name}:{repeat}:{arm}")
        os.environ["EE_M4_EVAL_CONTEXT"] = (
            f"{task_name}-r{repeat}-{arm}-heldout-h{checkpoint['horizon']}"
        )
        heldout = _remote_evaluate(
            candidate,
            spec,
            seeds,
            repeats=int(_conditions()["heldout_repeats"]),
            workers=int(_conditions()["evaluator_workers_per_active_run"]),
            cold=True,
        )
        evaluations.append(
            {
                "horizon": checkpoint["horizon"],
                "candidate_sha256": checkpoint["candidate_sha256"],
                "heldout": heldout,
            }
        )
    budget_valid = not bool(trajectory["budget_violation"])
    improvements = [
        max(0.0, float(item["heldout"]["raw_speedup"]) - 1.0)
        if item["heldout"]["correct"] and budget_valid
        else 0.0
        for item in evaluations
    ]
    final = evaluations[-1]["heldout"]
    final_valid = bool(final["correct"] and budget_valid)
    final_improvement = (
        max(0.0, float(final["raw_speedup"]) - 1.0) if final_valid else 0.0
    )
    arm_result = trajectory["final_arm_result"]
    payload = {
        "schema_version": "1.0",
        "task": task_name,
        "repeat": repeat,
        "arm": arm,
        "budget_valid": budget_valid,
        "final_valid": final_valid,
        "valid_final_heldout_improvement": final_improvement,
        "heldout_anytime_auc": sum(improvements) / len(improvements),
        "success": bool(final_valid and final_improvement > 0.0),
        "wall_seconds": float(arm_result["wall_seconds"]),
        "observed_tokens": int(arm_result["tokens"]),
        "checkpoint_evaluations": evaluations,
        "authority": "M4_HELDOUT_DECISION_EVIDENCE_ONLY",
    }
    blind._write_json(result_path, payload)
    return payload


def _rank_tuple(item: dict[str, Any]) -> tuple[float, ...]:
    return (
        float(bool(item["final_valid"])),
        float(item["valid_final_heldout_improvement"]),
        float(item["heldout_anytime_auc"]),
        float(bool(item["success"])),
        -float(item["wall_seconds"]),
        -float(item["observed_tokens"]),
    )


def _beats(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _rank_tuple(left) > _rank_tuple(right)


def _aggregate(run_root: Path, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    indexed = {
        (item["task"], int(item["repeat"]), item["arm"]): item for item in blocks
    }
    tasks = [str(item["task"]) for item in _load_protocol()["tasks"]]
    task_wins: dict[str, int] = {}
    for task in tasks:
        task_wins[task] = sum(
            _beats(
                indexed[(task, repeat, "evidence_evolve")],
                indexed[(task, repeat, "vanilla")],
            )
            for repeat in (1, 2, 3)
        )
    pairwise: dict[str, dict[str, Any]] = {}
    for comparator in ("vanilla", "shinka", "ada"):
        ee_wins = 0
        comparator_wins = 0
        comparator_task_wins = 0
        for task in tasks:
            task_comparator_wins = 0
            for repeat in (1, 2, 3):
                ee = indexed[(task, repeat, "evidence_evolve")]
                other = indexed[(task, repeat, comparator)]
                ee_wins += int(_beats(ee, other))
                comparator_wins += int(_beats(other, ee))
                task_comparator_wins += int(_beats(other, ee))
            comparator_task_wins += int(task_comparator_wins >= 2)
        pairwise[comparator] = {
            "ee_wins": ee_wins,
            "comparator_wins": comparator_wins,
            "ties": 9 - ee_wins - comparator_wins,
            "comparator_task_family_wins": comparator_task_wins,
            "ee_nonlosses": 9 - comparator_wins,
        }
    continue_gate = (
        sum(count >= 2 for count in task_wins.values()) >= 2
        and pairwise["shinka"]["ee_nonlosses"] >= 5
        and pairwise["ada"]["ee_nonlosses"] >= 5
    )
    stop_gate = any(
        row["comparator_wins"] >= 6 and row["comparator_task_family_wins"] >= 2
        for row in pairwise.values()
    )
    decision = (
        "CONTINUE_EE_SEARCH_RESEARCH"
        if continue_gate
        else "STOP_EE_SEARCH_CORE"
        if stop_gate
        else "SIMPLIFY_TO_SEARCH_HARNESS"
    )
    return {
        "schema_version": "1.0",
        "campaign": "m4_search_value_tournament_v0",
        "protocol_sha256": sha256_file(PROTOCOL),
        "portfolio_candidate_lock_sha256": sha256_file(
            run_root / "portfolio_candidate_lock.json"
        ),
        "task_family_ee_wins_vs_vanilla": task_wins,
        "pairwise": pairwise,
        "decision": decision,
        "continue_gate_met": continue_gate,
        "stop_gate_met": stop_gate,
        "ranking": _load_protocol()["ranking"],
        "blocks": blocks,
        "claim_scope": _load_protocol()["claim_scope"],
        "superiority_claim_permitted": False,
        "mechanism_claim_permitted": False,
    }


def finalize(run_root: Path, max_parallel: int) -> dict[str, Any]:
    lock_portfolio(run_root)
    work = [
        (str(task["task"]), repeat, arm)
        for task in _load_protocol()["tasks"]
        for repeat in (1, 2, 3)
        for arm in ARMS
    ]
    blocks: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(_evaluate_block, run_root, task, repeat, arm): (
                task,
                repeat,
                arm,
            )
            for task, repeat, arm in work
        }
        for future in as_completed(futures):
            blocks.append(future.result())
    blocks.sort(key=lambda item: (item["task"], item["repeat"], item["arm"]))
    result = _aggregate(run_root, blocks)
    blind._write_json(run_root / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 Search-Value Tournament")
    subparsers = parser.add_subparsers(dest="command", required=True)
    arm = subparsers.add_parser("arm")
    arm.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    arm.add_argument("--task", required=True)
    arm.add_argument("--repeat", type=int, choices=(1, 2, 3), required=True)
    arm.add_argument("--arm", choices=ARMS, required=True)
    search = subparsers.add_parser("search")
    search.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    search.add_argument("--max-parallel", type=int, default=4)
    finish = subparsers.add_parser("finalize")
    finish.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    finish.add_argument("--max-parallel", type=int, default=4)
    all_steps = subparsers.add_parser("all")
    all_steps.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    all_steps.add_argument("--max-parallel", type=int, default=4)
    remote = subparsers.add_parser("remote-evaluate")
    remote.add_argument("--task", required=True)
    remote.add_argument("--candidate", required=True)
    remote.add_argument("--seeds", required=True)
    remote.add_argument("--repeats", type=int, required=True)
    remote.add_argument("--workers", type=int, required=True)
    remote.add_argument("--cold", action="store_true")
    remote.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "remote-evaluate":
        result = run_remote_evaluator(
            task_name=args.task,
            candidate=Path(args.candidate),
            seeds_path=Path(args.seeds),
            repeats=args.repeats,
            workers=args.workers,
            cold=args.cold,
            output=Path(args.output),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        run_root = args.run_root.resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        if args.command == "arm":
            run_arm(run_root, args.task, args.repeat, args.arm)
        elif args.command == "search":
            run_search(run_root, args.max_parallel)
        elif args.command == "finalize":
            finalize(run_root, args.max_parallel)
        else:
            statuses = run_search(run_root, args.max_parallel)
            if any(item["state"] != "SUCCEEDED" for item in statuses):
                return 2
            finalize(run_root, args.max_parallel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
