from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.benchmarks import algotune_blind as blind
from evidence_evolve.benchmarks import engine_selection_r2_runner as base
from evidence_evolve.benchmarks.shinka_selection_audit import audit_shinka_selection
from evidence_evolve.hashing import sha256_file
from evidence_evolve.search.shinka_native import import_shinka_run


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "research/parity/engine_selection_shinka_postfix_confirmation.protocol.json"
UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_engine_selection_upstream/AlgoTuneTasks"
CAMPAIGN = "engine_selection_shinka_postfix_confirmation"
RUNNER_MODULE = "evidence_evolve.benchmarks.engine_selection_shinka_postfix_runner"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs" / CAMPAIGN
TASKS = ("pde_heat1d", "convex_hull", "communicability")
REPEATS = (1, 2)


def load_protocol() -> dict[str, Any]:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if protocol.get("campaign") != CAMPAIGN:
        raise ValueError("Shinka post-fix campaign drift")
    if tuple(item["task"] for item in protocol["tasks"]) != TASKS:
        raise ValueError("Shinka post-fix task drift")
    if protocol["confirmation"] != {
        "arms": ["shinka"],
        "repeats": [1, 2],
        "native_iterations": 12,
        "run_count": 6,
        "scheduling_unit": "ONE_TASK_ONE_SHINKA_RUN",
        "stop_stage_on_any_non_success_run": True,
    }:
        raise ValueError("Shinka post-fix confirmation design drift")
    if protocol["heldout"]["enabled"] or protocol["claim_scope"] != "DEVELOPMENT_MECHANICS_CONFIRMATION_ONLY":
        raise ValueError("Shinka post-fix confirmation cannot carry scientific authority")
    for task in protocol["tasks"]:
        source = UPSTREAM_ROOT / task["task"] / f"{task['task']}.py"
        if sha256_file(source) != task["source_sha256"]:
            raise ValueError(f"Shinka post-fix source drift: {task['task']}")
    for relative, expected in protocol["implementation_bindings"].items():
        if sha256_file(REPO_ROOT / relative) != expected:
            raise ValueError(f"Shinka post-fix implementation drift: {relative}")
    return protocol


def _install_context() -> None:
    base.PROTOCOL = PROTOCOL
    base.UPSTREAM_ROOT = UPSTREAM_ROOT
    base.CAMPAIGN = CAMPAIGN
    base.CAMPAIGN_SLUG = "shinka-postfix"
    base.RUNNER_MODULE = RUNNER_MODULE
    base.DEFAULT_RUN_ROOT = DEFAULT_RUN_ROOT
    base.load_protocol = load_protocol


def run_remote_evaluator(**kwargs: Any) -> dict[str, Any]:
    _install_context()
    return base.run_remote_evaluator(**kwargs)


def run_arm(run_root: Path, task: str, repeat: int) -> dict[str, Any]:
    if task not in TASKS or repeat not in REPEATS:
        raise ValueError("invalid Shinka post-fix run")
    _install_context()
    run_dir = run_root / task / f"repeat_{repeat:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    base._configure(run_dir, task, repeat)
    os.environ.update(
        {
            "EE_ENGINE_EVAL_CONTEXT": f"{task}-postfix-r{repeat}-shinka",
            "EE_ENGINE_RUN_ROOT": str(run_root),
            "EE_ENGINE_ARM": "shinka",
            "EE_ENGINE_REPEAT": str(repeat),
            "EE_ENGINE_ARM_STARTED_MONOTONIC": str(time.monotonic()),
        }
    )
    base._manifest(run_dir, task, repeat, "POST_FIX_CONFIRMATION")
    arm_dir = run_dir / "arms" / "shinka"
    result_path = arm_dir / "trajectory_result.json"
    audit_path = arm_dir / "selection_audit.json"
    if result_path.exists() and audit_path.exists():
        return json.loads(audit_path.read_text(encoding="utf-8"))
    arm_result = blind.run_shinka(run_dir)
    candidate = Path(arm_result["candidate_path"])
    trajectory = {
        "schema_version": "1.0",
        "campaign": CAMPAIGN,
        "task": task,
        "repeat": repeat,
        "stage": "POST_FIX_CONFIRMATION",
        "arm": "shinka",
        "run_valid": float(arm_result["wall_seconds"])
        <= float(load_protocol()["common_conditions"]["wall_seconds_per_run"]),
        "observed_tokens": int(arm_result["tokens"]),
        "tokens_account_only": True,
        "wall_seconds": float(arm_result["wall_seconds"]),
        "final_candidate_path": str(candidate.resolve()),
        "final_candidate_sha256": sha256_file(candidate),
        "final_development": arm_result["development"],
        "proposal_valid_rate": float(arm_result["proposal_valid_rate"]),
        "final_arm_result": arm_result,
    }
    blind._write_json(result_path, trajectory)
    imported_best = import_shinka_run(arm_dir / "upstream").best_program_id
    audit = audit_shinka_selection(
        arm_dir / "upstream" / "programs.sqlite",
        imported_best_program_id=imported_best,
        selected_candidate=candidate,
    )
    audit.update({"campaign": CAMPAIGN, "task": task, "repeat": repeat})
    blind._write_json(audit_path, audit)
    if audit["status"] != "PASS":
        raise RuntimeError(f"Shinka selection mechanics failed: {task}:r{repeat}")
    return audit


def summarize(run_root: Path) -> dict[str, Any]:
    audits = []
    for task in TASKS:
        for repeat in REPEATS:
            path = run_root / task / f"repeat_{repeat:02d}" / "arms/shinka/selection_audit.json"
            if not path.is_file():
                raise ValueError(f"missing Shinka post-fix audit: {task}:r{repeat}")
            audits.append(json.loads(path.read_text(encoding="utf-8")))
    result = {
        "schema_version": "1.0",
        "campaign": CAMPAIGN,
        "status": "PASS" if all(item["status"] == "PASS" for item in audits) else "FAIL",
        "scientific_authority": False,
        "protocol_sha256": sha256_file(PROTOCOL),
        "run_count": len(audits),
        "all_mechanics_gates_passed": all(
            all(item["gates"].values()) for item in audits
        ),
        "runs": [
            {
                "task": item["task"],
                "repeat": item["repeat"],
                "status": item["status"],
                "program_count": item["program_count"],
                "valid_program_count": item["valid_program_count"],
                "formal_best_program_id": item["formal_best_program_id"],
                "final_raw_speedup": next(
                    row["raw_speedup"]
                    for row in item["trajectory"]
                    if row["candidate_id"] == item["formal_best_program_id"]
                ),
            }
            for item in audits
        ],
        "claim_scope": "DEVELOPMENT_MECHANICS_CONFIRMATION_ONLY",
        "full_30_round_rematch_authorized": False,
    }
    blind._write_json(run_root / "result.json", result)
    return result


def _manifest(run_root: Path) -> dict[str, Any]:
    payload = {
        "schema_version": "1.0",
        "campaign": CAMPAIGN,
        "protocol_sha256": sha256_file(PROTOCOL),
        "tasks": list(TASKS),
        "repeats": list(REPEATS),
        "arm": "shinka",
        "native_iterations": 12,
        "heldout_enabled": False,
    }
    path = run_root / "manifest.json"
    if not path.exists():
        create_once_json(path, payload)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Shinka post-fix mechanics confirmation")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    run.add_argument("--task", choices=TASKS, required=True)
    run.add_argument("--repeat", type=int, choices=REPEATS, required=True)
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
    root = getattr(args, "run_root", DEFAULT_RUN_ROOT).resolve()
    if args.command == "run":
        _manifest(root)
        result = run_arm(root, args.task, args.repeat)
    elif args.command == "summarize":
        result = summarize(root)
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
