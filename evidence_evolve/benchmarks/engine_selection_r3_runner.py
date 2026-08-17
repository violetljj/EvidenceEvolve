from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import fmean, median
from typing import Any

from evidence_evolve.benchmarks import engine_selection_r2_runner as base
from evidence_evolve.hashing import sha256_file


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "research/parity/engine_selection_r3_development_screen.protocol.json"
UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_engine_selection_upstream/AlgoTuneTasks"
CAMPAIGN = "engine_selection_r3_development_screen"
RUNNER_MODULE = "evidence_evolve.benchmarks.engine_selection_r3_runner"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs" / CAMPAIGN
ARMS = ("vanilla", "ada", "shinka", "evox")


def load_protocol() -> dict[str, Any]:
    payload = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    validate_protocol(payload)
    return payload


def validate_protocol(protocol: dict[str, Any]) -> None:
    if protocol.get("campaign") != CAMPAIGN:
        raise ValueError("Engine Selection R3 campaign drift")
    if tuple(protocol.get("arms", ())) != ARMS:
        raise ValueError("Engine Selection R3 arm drift")
    if protocol["screen"] != {
        "repeats": [1],
        "run_count": 12,
        "scheduling_unit": "ONE_TASK_ALL_FOUR_ARMS",
        "stop_stage_on_any_non_success_block": True,
    }:
        raise ValueError("Engine Selection R3 screen drift")
    conditions = protocol["common_conditions"]
    if conditions["token_policy"] != "ACCOUNT_ONLY_NEVER_STOP_ITERATIONS_NEVER_INVALIDATE_RUN":
        raise ValueError("Engine Selection R3 token policy drift")
    if conditions["token_call_launch_ceiling"] is not None or conditions["token_hard_ceiling"] is not None:
        raise ValueError("Engine Selection R3 cannot contain a token stop")
    if protocol["heldout"]["enabled"] or protocol["ranking"]["formal_winner_permitted"]:
        raise ValueError("Engine Selection R3 development screen cannot claim a winner")
    if protocol["benchmark_policy"]["fresh_tasks_spent"] != 0:
        raise ValueError("Engine Selection R3 development screen cannot spend fresh tasks")
    tasks = protocol.get("tasks", [])
    if len(tasks) != 3 or len({item["category"] for item in tasks}) != 3:
        raise ValueError("Engine Selection R3 requires three heterogeneous development tasks")
    for task in tasks:
        source = UPSTREAM_ROOT / task["task"] / f"{task['task']}.py"
        if not source.is_file() or sha256_file(source) != task["source_sha256"]:
            raise ValueError(f"Engine Selection R3 source drift: {task['task']}")
    admission = protocol["transport_admission"]
    result = REPO_ROOT / admission["result"]
    if not result.is_file() or sha256_file(result) != admission["result_sha256"]:
        raise ValueError("Engine Selection R3 transport admission drift")
    payload = json.loads(result.read_text(encoding="utf-8"))
    if payload.get("status") != admission["required_status"] or payload.get("attempts_succeeded") != 20:
        raise ValueError("Engine Selection R3 transport admission did not pass")


def _install_context() -> None:
    base.PROTOCOL = PROTOCOL
    base.UPSTREAM_ROOT = UPSTREAM_ROOT
    base.CAMPAIGN = CAMPAIGN
    base.RUNNER_MODULE = RUNNER_MODULE
    base.DEFAULT_RUN_ROOT = DEFAULT_RUN_ROOT
    base.load_protocol = load_protocol


def run_arm(run_root: Path, task: str, repeat: int, arm: str) -> dict[str, Any]:
    if repeat != 1:
        raise ValueError("Engine Selection R3 development screen only permits repeat 1")
    _install_context()
    return base.run_arm(run_root, task, repeat, arm)


def search(run_root: Path, max_parallel: int) -> list[dict[str, Any]]:
    _install_context()
    tasks = [item["task"] for item in load_protocol()["tasks"]]
    items = [(task, 1, arm) for task in tasks for arm in ARMS]
    return base._run_parallel(run_root, items, max_parallel, "development_process_summary.json")


def summarize(run_root: Path) -> dict[str, Any]:
    protocol = load_protocol()
    by_arm: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
    for task in protocol["tasks"]:
        for arm in ARMS:
            path = run_root / task["task"] / "repeat_01" / "arms" / arm / "trajectory_result.json"
            if not path.is_file():
                raise ValueError(f"missing completed R3 trajectory: {task['task']}:{arm}")
            trajectory = json.loads(path.read_text(encoding="utf-8"))
            dev = trajectory["final_development"]
            valid = bool(dev["controls"]["candidate_valid"])
            raw = float(dev["metrics"]["raw_speedup"])
            by_arm[arm].append({
                "task": task["task"],
                "valid": valid,
                "raw_speedup": raw,
                "improvement": max(0.0, raw - 1.0) if valid else 0.0,
                "wall_seconds": float(trajectory["wall_seconds"]),
                "observed_tokens": int(trajectory["observed_tokens"]),
            })
    aggregates = {}
    for arm, records in by_arm.items():
        improvements = [item["improvement"] for item in records]
        aggregates[arm] = {
            "mean_improvement": fmean(improvements),
            "median_improvement": median(improvements),
            "minimum_improvement": min(improvements),
            "positive_task_count": sum(value > 0.0 for value in improvements),
            "mean_wall_seconds": fmean(item["wall_seconds"] for item in records),
            "total_tokens_account_only": sum(item["observed_tokens"] for item in records),
            "tasks": records,
        }
    order = sorted(
        ARMS,
        key=lambda arm: (
            aggregates[arm]["mean_improvement"],
            aggregates[arm]["median_improvement"],
            aggregates[arm]["minimum_improvement"],
            aggregates[arm]["positive_task_count"],
            -aggregates[arm]["mean_wall_seconds"],
            -aggregates[arm]["total_tokens_account_only"],
        ),
        reverse=True,
    )
    result = {
        "schema_version": "1.0",
        "campaign": CAMPAIGN,
        "status": "DEVELOPMENT_SCREEN_COMPLETE",
        "scientific_authority": False,
        "protocol_sha256": sha256_file(PROTOCOL),
        "development_signal_order": order,
        "aggregates": aggregates,
        "token_role": "accounting and last tie-break only",
        "winner_claim_permitted": False,
        "next_action": "Freeze a separate signal gate before spending any fresh blind task.",
    }
    base.blind._write_json(run_root / "development_result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="visible Engine Selection R3 development screen")
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
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    if args.command == "arm":
        result = run_arm(run_root, args.task, args.repeat, args.arm)
    elif args.command == "search":
        result = search(run_root, args.max_parallel)
    else:
        result = summarize(run_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
