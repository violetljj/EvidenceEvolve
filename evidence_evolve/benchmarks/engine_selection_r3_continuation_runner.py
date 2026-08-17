from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from statistics import fmean, median
from typing import Any

from evidence_evolve.benchmarks import algotune_blind as blind
from evidence_evolve.benchmarks import engine_selection_r2_runner as base
from evidence_evolve.benchmarks import engine_selection_r3_runner as r3
from evidence_evolve.hashing import sha256_file, sha256_object


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "research/parity/engine_selection_r3_continuation_30.protocol.json"
UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_engine_selection_upstream/AlgoTuneTasks"
CAMPAIGN = "engine_selection_r3_continuation_30"
RUNNER_MODULE = "evidence_evolve.benchmarks.engine_selection_r3_continuation_runner"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs" / CAMPAIGN
ARMS = ("vanilla", "ada", "shinka", "evox")


def _directory_hash(path: Path) -> str:
    return sha256_object(
        {str(item.relative_to(path)): sha256_file(item) for item in sorted(path.rglob("*")) if item.is_file()}
    )


def load_protocol(*, validate_parent: bool = True) -> dict[str, Any]:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if protocol.get("campaign") != CAMPAIGN:
        raise ValueError("continuation campaign drift")
    if tuple(protocol.get("arms", ())) != ARMS:
        raise ValueError("continuation arm drift")
    if protocol["continuation"]["additional_native_iterations"] != 30:
        raise ValueError("continuation iteration budget drift")
    conditions = protocol["common_conditions"]
    if conditions["token_hard_ceiling"] is not None or conditions["token_call_launch_ceiling"] is not None:
        raise ValueError("continuation cannot contain a token stop")
    parent = REPO_ROOT / protocol["parent"]["run_root"]
    for task in protocol["tasks"]:
        source = UPSTREAM_ROOT / task["task"] / f"{task['task']}.py"
        if sha256_file(source) != task["source_sha256"]:
            raise ValueError(f"continuation source drift: {task['task']}")
        if not validate_parent:
            continue
        for arm in ARMS:
            arm_dir = parent / task["task"] / "repeat_01" / "arms" / arm
            binding = protocol["parent_bindings"][task["task"]][arm]
            if sha256_file(arm_dir / "selected_candidate.py") != binding["selected_candidate_sha256"]:
                raise ValueError(f"continuation parent candidate drift: {task['task']}:{arm}")
            if sha256_file(arm_dir / "trajectory_result.json") != binding["trajectory_sha256"]:
                raise ValueError(f"continuation parent trajectory drift: {task['task']}:{arm}")
            if arm == "vanilla":
                observed = sha256_file(arm_dir / "history.json")
            elif arm == "shinka":
                observed = sha256_file(arm_dir / "upstream" / "programs.sqlite")
            else:
                observed = _directory_hash(arm_dir / "upstream" / "checkpoints" / "checkpoint_12")
            if observed != binding["state_sha256"]:
                raise ValueError(f"continuation parent state drift: {task['task']}:{arm}")
    return protocol


def _install_context() -> None:
    base.PROTOCOL = PROTOCOL
    base.UPSTREAM_ROOT = UPSTREAM_ROOT
    base.CAMPAIGN = CAMPAIGN
    base.CAMPAIGN_SLUG = "engine-r3-cont30"
    base.RUNNER_MODULE = RUNNER_MODULE
    base.DEFAULT_RUN_ROOT = DEFAULT_RUN_ROOT
    # The AutoDL worker is execution-only and intentionally does not receive the
    # parent run tree. Parent bindings are verified locally before dispatch.
    base.load_protocol = lambda: load_protocol(validate_parent=False)


def run_remote_evaluator(**kwargs: Any) -> dict[str, Any]:
    _install_context()
    return base.run_remote_evaluator(**kwargs)


def _parent_arm(task: str, arm: str) -> Path:
    protocol = load_protocol()
    return REPO_ROOT / protocol["parent"]["run_root"] / task / "repeat_01" / "arms" / arm


def _resume_sky(run_dir: Path, task: str, arm: str) -> dict[str, Any]:
    from skydiscover.runner import Runner

    checkpoint = _parent_arm(task, arm) / "upstream" / "checkpoints" / "checkpoint_12"
    original_run = Runner.run

    async def resumed_run(
        runner: Runner, iterations: int | None = None, checkpoint_path: str | None = None
    ) -> Any:
        return await original_run(runner, iterations=iterations, checkpoint_path=str(checkpoint))

    Runner.run = resumed_run
    try:
        return blind.run_ada(run_dir) if arm == "ada" else blind.run_evox(run_dir)
    finally:
        Runner.run = original_run


def _resume_shinka(run_dir: Path, task: str) -> dict[str, Any]:
    source = _parent_arm(task, "shinka") / "upstream"
    destination = run_dir / "arms" / "shinka" / "upstream"
    if not destination.exists():
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("evidence_evolve"))
    connection = sqlite3.connect(destination / "programs.sqlite")
    try:
        parent_max = int(connection.execute("SELECT MAX(generation) FROM programs").fetchone()[0])
    finally:
        connection.close()
    additional = int(load_protocol()["continuation"]["additional_native_iterations"])
    previous_generations = blind.GENERATIONS
    blind.GENERATIONS = parent_max + additional
    try:
        return blind.run_shinka(run_dir)
    finally:
        blind.GENERATIONS = previous_generations


def run_arm(run_root: Path, task: str, repeat: int, arm: str) -> dict[str, Any]:
    if repeat != 1 or arm not in ARMS:
        raise ValueError("invalid continuation arm")
    _install_context()
    run_dir = run_root / task / "repeat_01"
    run_dir.mkdir(parents=True, exist_ok=True)
    base._configure(run_dir, task, repeat)
    os.environ.update({
        "EE_ENGINE_EVAL_CONTEXT": f"{task}-continuation-{arm}",
        "EE_ENGINE_RUN_ROOT": str(run_root),
        "EE_ENGINE_ARM": arm,
        "EE_ENGINE_REPEAT": "1",
        "EE_ENGINE_ARM_STARTED_MONOTONIC": str(time.monotonic()),
    })
    base._manifest(run_dir, task, repeat, "CONTINUATION_30")
    arm_dir = run_dir / "arms" / arm
    result_path = arm_dir / "trajectory_result.json"
    if result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    parent = _parent_arm(task, arm)
    parent_trajectory = json.loads((parent / "trajectory_result.json").read_text(encoding="utf-8"))
    parent_raw = float(parent_trajectory["final_development"]["metrics"]["raw_speedup"])
    started = time.perf_counter()
    if arm == "vanilla":
        previous_initial = blind.INITIAL
        blind.INITIAL = parent / "selected_candidate.py"
        try:
            arm_result = blind.run_vanilla(run_dir)
        finally:
            blind.INITIAL = previous_initial
    elif arm in ("ada", "evox"):
        arm_result = _resume_sky(run_dir, task, arm)
    else:
        arm_result = _resume_shinka(run_dir, task)
    candidate = Path(arm_result["candidate_path"])
    final_raw = float(arm_result["development"]["metrics"]["raw_speedup"])
    payload = {
        "schema_version": "1.0",
        "campaign": CAMPAIGN,
        "task": task,
        "repeat": 1,
        "stage": "CONTINUATION_30",
        "arm": arm,
        "run_valid": float(arm_result["wall_seconds"]) <= float(load_protocol()["common_conditions"]["wall_seconds_per_run"]),
        "observed_tokens": int(arm_result["tokens"]),
        "tokens_account_only": True,
        "wall_seconds": float(arm_result["wall_seconds"]),
        "parent_candidate_sha256": sha256_file(parent / "selected_candidate.py"),
        "parent_development_raw_speedup": parent_raw,
        "final_candidate_path": str(candidate.resolve()),
        "final_candidate_sha256": sha256_file(candidate),
        "final_development": arm_result["development"],
        "continuation_delta_raw_speedup": final_raw - parent_raw,
        "proposal_valid_rate": float(arm_result["proposal_valid_rate"]),
        "final_arm_result": arm_result,
        "runner_elapsed_seconds": time.perf_counter() - started,
    }
    blind._write_json(result_path, payload)
    return payload


def search(run_root: Path, max_parallel: int) -> list[dict[str, Any]]:
    _install_context()
    items = [(task["task"], 1, arm) for task in load_protocol()["tasks"] for arm in ARMS]
    return base._run_parallel(run_root, items, max_parallel, "continuation_process_summary.json")


def summarize(run_root: Path) -> dict[str, Any]:
    protocol = load_protocol()
    aggregates: dict[str, Any] = {}
    for arm in ARMS:
        rows = []
        for task in protocol["tasks"]:
            trajectory = json.loads(
                (run_root / task["task"] / "repeat_01" / "arms" / arm / "trajectory_result.json").read_text(encoding="utf-8")
            )
            raw = float(trajectory["final_development"]["metrics"]["raw_speedup"])
            rows.append({
                "task": task["task"],
                "raw_speedup": raw,
                "improvement": max(0.0, raw - 1.0),
                "parent_raw_speedup": float(trajectory["parent_development_raw_speedup"]),
                "continuation_delta_raw_speedup": float(trajectory["continuation_delta_raw_speedup"]),
                "tokens_account_only": int(trajectory["observed_tokens"]),
            })
        improvements = [row["improvement"] for row in rows]
        aggregates[arm] = {
            "mean_improvement": fmean(improvements),
            "median_improvement": median(improvements),
            "minimum_improvement": min(improvements),
            "positive_task_count": sum(value > 0 for value in improvements),
            "continuation_tokens_account_only": sum(row["tokens_account_only"] for row in rows),
            "tasks": rows,
        }
    result = {
        "schema_version": "1.0",
        "campaign": CAMPAIGN,
        "status": "CONTINUATION_COMPLETE",
        "scientific_authority": False,
        "protocol_sha256": sha256_file(PROTOCOL),
        "aggregates": aggregates,
        "winner_claim_permitted": False,
    }
    blind._write_json(run_root / "continuation_result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Engine Selection R3 plus-30 continuation")
    commands = parser.add_subparsers(dest="command", required=True)
    arm = commands.add_parser("arm")
    arm.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    arm.add_argument("--task", required=True)
    arm.add_argument("--repeat", type=int, choices=(1,), default=1)
    arm.add_argument("--arm", choices=ARMS, required=True)
    search_command = commands.add_parser("search")
    search_command.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    search_command.add_argument("--max-parallel", type=int, default=4)
    summary = commands.add_parser("summarize")
    summary.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    remote = commands.add_parser("remote-evaluate")
    remote.add_argument("--task", required=True)
    remote.add_argument("--candidate", required=True)
    remote.add_argument("--seeds", required=True)
    remote.add_argument("--repeats", type=int, required=True)
    remote.add_argument("--workers", type=int, required=True)
    remote.add_argument("--cold", action="store_true")
    remote.add_argument("--output", required=True)
    args = parser.parse_args()
    run_root = getattr(args, "run_root", DEFAULT_RUN_ROOT).resolve()
    if args.command == "arm":
        result = run_arm(run_root, args.task, args.repeat, args.arm)
    elif args.command == "search":
        result = search(run_root, args.max_parallel)
    elif args.command == "summarize":
        result = summarize(run_root)
    else:
        result = run_remote_evaluator(
            task_name=args.task,
            candidate=Path(args.candidate),
            seeds_path=Path(args.seeds),
            repeats=args.repeats,
            workers=args.workers,
            cold=args.cold,
            output=Path(args.output),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
