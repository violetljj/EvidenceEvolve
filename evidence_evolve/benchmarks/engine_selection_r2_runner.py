from __future__ import annotations

import argparse
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
from evidence_evolve.benchmarks import engine_selection_r1_runner as shared
from evidence_evolve.benchmarks.engine_selection_r2 import (
    PROTOCOL,
    REPO_ROOT,
    UPSTREAM_ROOT,
    load_protocol,
    score_final,
    score_round_1,
)
from evidence_evolve.benchmarks.algotune_official import OfficialTaskSpec
from evidence_evolve.hashing import sha256_file


CAMPAIGN = "engine_selection_r2_effect_first"
RUNNER_MODULE = "evidence_evolve.benchmarks.engine_selection_r2_runner"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs" / CAMPAIGN
ARMS = ("vanilla", "ada", "shinka", "evox")
SMOKE_TASK = "min_weight_assignment"
M4_UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_m4_upstream/AlgoTuneTasks"
NO_TOKEN_STOP = 2**63 - 1


def _install_shared_context() -> None:
    shared.PROTOCOL = PROTOCOL
    shared.DEFAULT_RUN_ROOT = DEFAULT_RUN_ROOT
    shared.UPSTREAM_ROOT = UPSTREAM_ROOT
    shared.CAMPAIGN = CAMPAIGN
    shared.load_protocol = load_protocol
    os.environ["EE_ENGINE_SELECTION_RUNNER_MODULE"] = RUNNER_MODULE
    os.environ["EE_ENGINE_SELECTION_CAMPAIGN_SLUG"] = "engine-r2"


def _task_payload(task_name: str) -> dict[str, Any]:
    for task in load_protocol()["tasks"]:
        if task["task"] == task_name:
            return task
    if task_name == SMOKE_TASK:
        previous = json.loads(
            (REPO_ROOT / "research/parity/m4_search_value_tournament_v6.protocol.json").read_text()
        )
        task = next(item for item in previous["tasks"] if item["task"] == task_name)
        return {**task, "role": "smoke"}
    raise ValueError(f"unknown Engine Selection R2 task: {task_name}")


def _source_path(task_name: str) -> Path:
    root = M4_UPSTREAM_ROOT if task_name == SMOKE_TASK else UPSTREAM_ROOT
    return root / task_name / f"{task_name}.py"


def _task_spec(task: dict[str, Any]) -> OfficialTaskSpec:
    return OfficialTaskSpec(
        name=str(task["task"]),
        class_name=str(task["class"]),
        problem_size=int(task["problem_size"]),
        source_path=str(_source_path(str(task["task"]))),
    )


def _conditions(task_name: str) -> dict[str, Any]:
    common = dict(load_protocol()["common_conditions"])
    if task_name == SMOKE_TASK:
        smoke = load_protocol()["mechanics_smoke"]
        common["max_native_search_iterations"] = int(smoke["max_search_iterations"])
        common["wall_seconds_per_run"] = int(smoke["wall_seconds"])
    return common


def _validate_provider() -> Path:
    conditions = load_protocol()["common_conditions"]
    executable = Path(str(conditions["provider_executable"]))
    if not executable.is_file():
        raise ValueError(f"pinned Codex executable missing: {executable}")
    completed = subprocess.run(
        [str(executable), "--version"], check=False, capture_output=True, text=True
    )
    observed = completed.stdout.strip()
    if completed.returncode != 0 or observed != conditions["provider_version"]:
        raise ValueError(f"pinned Codex version drift: {observed!r}")
    return executable


def run_remote_evaluator(
    *, task_name: str, candidate: Path, seeds_path: Path, repeats: int,
    workers: int, cold: bool, output: Path,
) -> dict[str, Any]:
    _install_shared_context()
    shared._task_payload = _task_payload
    shared._source_path = _source_path
    return shared.run_remote_evaluator(
        task_name=task_name,
        candidate=candidate,
        seeds_path=seeds_path,
        repeats=repeats,
        workers=workers,
        cold=cold,
        output=output,
    )


def remote_development_evaluate(
    candidate: str | Path, spec: OfficialTaskSpec, *, workers: int | None = None
) -> dict[str, Any]:
    _install_shared_context()
    shared._task_payload = _task_payload
    shared._source_path = _source_path
    conditions = _conditions(spec.name)
    start = int(os.environ.get("EE_ALGOTUNE_DEV_START", "0"))
    count = int(os.environ.get("EE_ALGOTUNE_DEV_COUNT", "20"))
    raw = shared._remote_evaluate(
        Path(candidate), spec, list(range(start, start + count)),
        repeats=int(os.environ.get("EE_ALGOTUNE_DEV_REPEATS", "2")),
        workers=workers or int(conditions["evaluator_workers_per_active_run"]),
        cold=False,
        context=os.environ.get("EE_ENGINE_EVAL_CONTEXT", "unscoped-development"),
    )
    return {
        "mechanics_status": "PASS",
        "metrics": {
            "invalid_solution_rate": 1.0 - float(raw["valid_rate"]),
            "raw_speedup": float(raw["raw_speedup"]),
        },
        "controls": {"candidate_valid": bool(raw["correct"]), "development_only": True},
        "error": str(raw.get("failure", "")),
        "remote_receipt_sha256": raw["remote_receipt_sha256"],
    }


def _configure(run_dir: Path, task_name: str, repeat: int) -> OfficialTaskSpec:
    _install_shared_context()
    protocol = load_protocol()
    task = _task_payload(task_name)
    conditions = _conditions(task_name)
    provider = _validate_provider()
    seed_count = int(conditions["development_seeds_per_repeat"])
    os.environ.update({
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "EE_ALGOTUNE_DEV_START": str((repeat - 1) * seed_count),
        "EE_ALGOTUNE_DEV_COUNT": str(seed_count),
        "EE_ALGOTUNE_DEV_REPEATS": str(conditions["development_repeats"]),
        "EE_ALGOTUNE_WORKERS": str(conditions["evaluator_workers_per_active_run"]),
        "EE_HETERO_GENERATIONS": str(conditions["max_native_search_iterations"]),
        "EE_M4_REMOTE_EVALUATOR": "1",
        "EE_ALGOTUNE_REMOTE_MODULE": "engine_selection_r2",
        "EE_SEARCH_TOKEN_LAUNCH_CEILING": str(NO_TOKEN_STOP),
        "EE_EVOX_PROTOCOL_NATIVE": "1",
        "EE_ENGINE_EVAL_CONTEXT": f"{task_name}-r{repeat}",
        "EVIDENCE_EVOLVE_CODEX_EXECUTABLE": str(provider),
    })
    heterogeneous.PROTOCOL = PROTOCOL
    heterogeneous.UPSTREAM_ROOT = (
        M4_UPSTREAM_ROOT.parent if task_name == SMOKE_TASK else UPSTREAM_ROOT.parent
    )
    heterogeneous._load_protocol = lambda: protocol
    heterogeneous._development = remote_development_evaluate
    spec = heterogeneous._configure(run_dir, task)
    blind.MODEL = str(conditions["model"])
    blind.REASONING_EFFORT = str(conditions["reasoning_effort"])
    blind.GENERATIONS = int(conditions["max_native_search_iterations"])
    blind.TOKEN_CEILING = NO_TOKEN_STOP
    blind.WALL_CEILING_SECONDS = float(conditions["wall_seconds_per_run"])
    blind.EVALUATOR_WORKERS = int(conditions["evaluator_workers_per_active_run"])
    return spec


def _manifest(run_dir: Path, task_name: str, repeat: int, stage: str) -> None:
    payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL),
        "task": task_name,
        "repeat": repeat,
        "stage": stage,
        "arms": list(ARMS),
        "conditions": _conditions(task_name),
        "token_stop_enabled": False,
    }
    path = run_dir / "engine_selection_manifest.json"
    if path.exists():
        existing = json.loads(path.read_text())
        for key in ("protocol_sha256", "task", "repeat", "stage", "conditions", "token_stop_enabled"):
            if existing.get(key) != payload[key]:
                raise ValueError(f"Engine Selection R2 manifest drift: {key}")
        return
    create_once_json(path, payload)


def run_arm(run_root: Path, task_name: str, repeat: int, arm: str) -> dict[str, Any]:
    if arm not in ARMS or repeat not in (1, 2, 3):
        raise ValueError("invalid Engine Selection R2 arm or repeat")
    stage = "MECHANICS_SMOKE" if task_name == SMOKE_TASK else "ROUND_1" if repeat == 1 else "FINAL"
    run_dir = run_root / task_name / f"repeat_{repeat:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _configure(run_dir, task_name, repeat)
    os.environ["EE_ENGINE_EVAL_CONTEXT"] = f"{task_name}-r{repeat}-{arm}"
    _manifest(run_dir, task_name, repeat, stage)
    arm_dir = run_dir / "arms" / arm
    trajectory_path = arm_dir / "trajectory_result.json"
    if trajectory_path.exists():
        return json.loads(trajectory_path.read_text())
    started = time.perf_counter()
    if arm == "vanilla":
        arm_result = blind.run_vanilla(run_dir)
    elif arm == "ada":
        arm_result = blind.run_ada(run_dir)
    elif arm == "shinka":
        arm_result = blind.run_shinka(run_dir)
    else:
        arm_result = blind.run_evox(run_dir)
    candidate = Path(arm_result["candidate_path"])
    if not candidate.is_file():
        raise ValueError("final candidate missing")
    conditions = _conditions(task_name)
    payload = {
        "schema_version": "1.0",
        "task": task_name,
        "repeat": repeat,
        "stage": stage,
        "arm": arm,
        "model": blind.MODEL,
        "reasoning_effort": blind.REASONING_EFFORT,
        "run_valid": float(arm_result["wall_seconds"]) <= float(conditions["wall_seconds_per_run"]),
        "invalid_reason": None,
        "observed_tokens": int(arm_result["tokens"]),
        "tokens_account_only": True,
        "wall_seconds": float(arm_result["wall_seconds"]),
        "final_candidate_path": str(candidate.resolve()),
        "final_candidate_sha256": sha256_file(candidate),
        "final_development": arm_result["development"],
        "proposal_valid_rate": float(arm_result["proposal_valid_rate"]),
        "final_arm_result": arm_result,
        "runner_elapsed_seconds": time.perf_counter() - started,
    }
    blind._write_json(trajectory_path, payload)
    return payload


def _command(run_root: Path, task: str, repeat: int, arm: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        RUNNER_MODULE,
        "arm",
        "--run-root",
        str(run_root),
        "--task",
        task,
        "--repeat",
        str(repeat),
        "--arm",
        arm,
    ]


def _run_item(run_root: Path, task: str, repeat: int, arm: str) -> dict[str, Any]:
    run_dir = run_root / task / f"repeat_{repeat:02d}"
    process_dir = run_dir / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status = process_dir / f"{arm}.json"
    if status.exists():
        return json.loads(status.read_text())
    stdout = process_dir / f"{arm}.stdout.log"
    stderr = process_dir / f"{arm}.stderr.log"
    conditions = _conditions(task)
    started = time.monotonic()
    state = "FAILED"
    returncode: int | None = None
    try:
        with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
            completed = subprocess.run(
                _command(run_root, task, repeat, arm),
                cwd=REPO_ROOT,
                stdout=out,
                stderr=err,
                timeout=float(conditions["wall_seconds_per_run"]) + float(conditions["wall_grace_seconds"]),
                env={**os.environ, "PYTHONUTF8": "1"},
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
        "stdout_sha256": sha256_file(stdout),
        "stderr_sha256": sha256_file(stderr),
    }
    blind._write_json(status, payload)
    return payload


def _run_parallel(run_root: Path, items: list[tuple[str, int, str]], max_parallel: int, output: str) -> list[dict[str, Any]]:
    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_run_item, run_root, *item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    blind._write_json(run_root / output, results)
    return results


def run_mechanics_smoke(run_root: Path, max_parallel: int) -> dict[str, Any]:
    smoke_root = run_root / str(load_protocol()["mechanics_smoke"]["attempt"])
    items = [(SMOKE_TASK, 1, arm) for arm in ARMS]
    statuses = _run_parallel(smoke_root, items, max_parallel, "process_summary.json")
    trajectories = []
    for arm in ARMS:
        path = smoke_root / SMOKE_TASK / "repeat_01" / "arms" / arm / "trajectory_result.json"
        if path.exists():
            trajectories.append(json.loads(path.read_text()))
    passed = len(trajectories) == 4 and all(item["state"] == "SUCCEEDED" for item in statuses)
    payload = {
        "schema_version": "1.0",
        "campaign": CAMPAIGN,
        "stage": "MECHANICS_SMOKE",
        "scientific_authority": False,
        "protocol_sha256": sha256_file(PROTOCOL),
        "token_stop_enabled": False,
        "status": "PASS" if passed else "FAIL",
        "formal_search_admitted": passed,
        "statuses": sorted(statuses, key=lambda item: item["arm"]),
        "arms": sorted(trajectories, key=lambda item: item["arm"]),
    }
    blind._write_json(run_root / load_protocol()["mechanics_smoke"]["receipt"], payload)
    return payload


def search_round_1(run_root: Path, max_parallel: int) -> list[dict[str, Any]]:
    receipt = run_root / load_protocol()["mechanics_smoke"]["receipt"]
    if not receipt.exists() or json.loads(receipt.read_text()).get("status") != "PASS":
        raise ValueError("passing mechanics smoke required")
    items = [(task["task"], 1, arm) for task in load_protocol()["tasks"] for arm in ARMS]
    return _run_parallel(run_root, items, max_parallel, "round_1_process_summary.json")


def search_final(run_root: Path, max_parallel: int) -> list[dict[str, Any]]:
    result_path = run_root / "round_1_result.json"
    if not result_path.exists():
        raise ValueError("round-one result required")
    result = json.loads(result_path.read_text())
    finalists = [str(item) for item in result["finalists"]]
    items = [
        (task["task"], repeat, arm)
        for task in load_protocol()["tasks"]
        for repeat in (2, 3)
        for arm in finalists
    ]
    return _run_parallel(run_root, items, max_parallel, "final_process_summary.json")


def _lock_stage(run_root: Path, stage: str, arms: list[str], repeats: tuple[int, ...]) -> dict[str, Any]:
    if any((run_root / task["task"]).rglob(f"heldout_{stage.lower()}_seeds.json") for task in load_protocol()["tasks"]):
        raise ValueError("heldout seeds existed before candidate lock")
    entries = []
    for task in load_protocol()["tasks"]:
        for repeat in repeats:
            for arm in arms:
                path = run_root / task["task"] / f"repeat_{repeat:02d}" / "arms" / arm / "trajectory_result.json"
                if not path.exists():
                    raise ValueError(f"missing trajectory: {task['task']}:{repeat}:{arm}")
                trajectory = json.loads(path.read_text())
                candidate = Path(trajectory["final_candidate_path"])
                if sha256_file(candidate) != trajectory["final_candidate_sha256"]:
                    raise ValueError("final candidate drift")
                entries.append({
                    "task": task["task"],
                    "repeat": repeat,
                    "arm": arm,
                    "trajectory_sha256": sha256_file(path),
                    "candidate_path": str(candidate),
                    "candidate_sha256": trajectory["final_candidate_sha256"],
                })
    payload = {
        "schema_version": "1.0",
        "stage": stage,
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL),
        "heldout_existed_at_lock": False,
        "entries": entries,
    }
    path = run_root / f"{stage.lower()}_candidate_lock.json"
    if not path.exists():
        create_once_json(path, payload)
    return json.loads(path.read_text())


def _heldout_seeds(run_root: Path, task: str, repeat: int, stage: str) -> list[int]:
    path = run_root / task / f"repeat_{repeat:02d}" / f"heldout_{stage.lower()}_seeds.json"
    if path.exists():
        return [int(value) for value in json.loads(path.read_text())["seeds"]]
    lock = run_root / f"{stage.lower()}_candidate_lock.json"
    if not lock.exists():
        raise ValueError("candidate lock required before heldout seed generation")
    count = int(load_protocol()["common_conditions"]["heldout_seeds_per_repeat"])
    excluded = set(range(60))
    values: set[int] = set()
    while len(values) < count:
        value = secrets.randbelow(2**32)
        if value not in excluded:
            values.add(value)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_after_lock": True,
        "lock_sha256": sha256_file(lock),
        "seeds": sorted(values),
    }
    create_once_json(path, payload)
    return payload["seeds"]


def _evaluate_block(run_root: Path, task: str, repeat: int, arm: str, stage: str) -> dict[str, Any]:
    run_dir = run_root / task / f"repeat_{repeat:02d}"
    result_path = run_dir / "heldout" / f"{stage.lower()}_{arm}.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    trajectory = json.loads((run_dir / "arms" / arm / "trajectory_result.json").read_text())
    candidate = Path(trajectory["final_candidate_path"])
    if sha256_file(candidate) != trajectory["final_candidate_sha256"]:
        raise ValueError("candidate drift before heldout")
    _install_shared_context()
    shared._task_payload = _task_payload
    shared._source_path = _source_path
    heldout = shared._remote_evaluate(
        candidate,
        _task_spec(_task_payload(task)),
        _heldout_seeds(run_root, task, repeat, stage),
        repeats=int(load_protocol()["common_conditions"]["heldout_repeats"]),
        workers=int(load_protocol()["common_conditions"]["evaluator_workers_per_active_run"]),
        cold=True,
        context=f"{CAMPAIGN}-{stage}-{task}-r{repeat}-{arm}",
    )
    payload = {
        "task": task,
        "repeat": repeat,
        "arm": arm,
        "stage": stage,
        "run_valid": bool(trajectory["run_valid"]),
        "observed_tokens": int(trajectory["observed_tokens"]),
        "wall_seconds": float(trajectory["wall_seconds"]),
        "heldout": heldout,
        "authority": "ENGINE_SELECTION_R2_HELDOUT_DECISION_EVIDENCE_ONLY",
    }
    blind._write_json(result_path, payload)
    return payload


def _finalize_stage(run_root: Path, stage: str, arms: list[str], repeats: tuple[int, ...], max_parallel: int) -> list[dict[str, Any]]:
    _lock_stage(run_root, stage, arms, repeats)
    tasks = [item["task"] for item in load_protocol()["tasks"]]
    for task in tasks:
        for repeat in repeats:
            _heldout_seeds(run_root, task, repeat, stage)
    work = [(task, repeat, arm, stage) for task in tasks for repeat in repeats for arm in arms]
    blocks: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_evaluate_block, run_root, *item) for item in work]
        for future in as_completed(futures):
            blocks.append(future.result())
    return sorted(blocks, key=lambda item: (item["task"], item["repeat"], item["arm"]))


def finalize_round_1(run_root: Path, max_parallel: int) -> dict[str, Any]:
    blocks = _finalize_stage(run_root, "ROUND_1", list(ARMS), (1,), max_parallel)
    result = score_round_1(blocks)
    blind._write_json(run_root / "round_1_blocks.json", blocks)
    blind._write_json(run_root / "round_1_result.json", result)
    return result


def finalize_final(run_root: Path, max_parallel: int) -> dict[str, Any]:
    round_1 = json.loads((run_root / "round_1_result.json").read_text())
    finalists = [str(item) for item in round_1["finalists"]]
    blocks = _finalize_stage(run_root, "FINAL", finalists, (2, 3), max_parallel)
    result = score_final(round_1, blocks)
    result["round_1_result_sha256"] = sha256_file(run_root / "round_1_result.json")
    blind._write_json(run_root / "final_blocks.json", blocks)
    blind._write_json(run_root / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="effect-first Engine Selection R2")
    subparsers = parser.add_subparsers(dest="command", required=True)
    arm = subparsers.add_parser("arm")
    arm.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    arm.add_argument("--task", required=True)
    arm.add_argument("--repeat", type=int, choices=(1, 2, 3), required=True)
    arm.add_argument("--arm", choices=ARMS, required=True)
    for name in ("smoke", "search-round-1", "finalize-round-1", "search-final", "finalize-final"):
        command = subparsers.add_parser(name)
        command.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
        command.add_argument("--max-parallel", type=int, default=4)
    remote = subparsers.add_parser("remote-evaluate")
    remote.add_argument("--task", required=True)
    remote.add_argument("--candidate", required=True)
    remote.add_argument("--seeds", required=True)
    remote.add_argument("--repeats", type=int, required=True)
    remote.add_argument("--workers", type=int, required=True)
    remote.add_argument("--cold", action="store_true")
    remote.add_argument("--output", required=True)
    args = parser.parse_args()
    root = getattr(args, "run_root", DEFAULT_RUN_ROOT).resolve()
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
    elif args.command == "arm":
        result = run_arm(root, args.task, args.repeat, args.arm)
    elif args.command == "smoke":
        result = run_mechanics_smoke(root, args.max_parallel)
    elif args.command == "search-round-1":
        result = search_round_1(root, args.max_parallel)
    elif args.command == "finalize-round-1":
        result = finalize_round_1(root, args.max_parallel)
    elif args.command == "search-final":
        result = search_final(root, args.max_parallel)
    else:
        result = finalize_final(root, args.max_parallel)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
