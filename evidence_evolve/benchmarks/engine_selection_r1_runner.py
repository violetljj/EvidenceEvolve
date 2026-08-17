from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
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
from evidence_evolve.benchmarks.engine_selection_r1 import (
    PROTOCOL,
    REPO_ROOT,
    UPSTREAM_ROOT,
    load_protocol,
    score_core,
    score_reserve,
)
from evidence_evolve.benchmarks.algotune_official import (
    OfficialTaskSpec,
    evaluate_official_candidate,
    evaluate_official_candidate_cold,
)
from evidence_evolve.hashing import sha256_file
from evidence_evolve.remote_cpu import (
    RemoteEntrypoint,
    create_job_request,
    dispatch_job,
    verify_result,
)


CAMPAIGN = "engine_selection_r1"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs" / CAMPAIGN
ARMS = ("vanilla", "ada", "shinka", "evox")
CHECKPOINTS = (50_000, 100_000, 200_000)
REMOTE_HOST = "root@connect.westb.seetacloud.com"
REMOTE_PORT = 16288
REMOTE_ROOT = "/root/autodl-tmp/evidence-evolve-worker"
SMOKE_TASK = "min_weight_assignment"
M4_UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_m4_upstream/AlgoTuneTasks"


def _task_payload(task_name: str) -> dict[str, Any]:
    for task in load_protocol()["tasks"]:
        if task["task"] == task_name:
            return task
    if task_name == SMOKE_TASK:
        protocol = json.loads(
            (REPO_ROOT / "research/parity/m4_search_value_tournament_v6.protocol.json").read_text()
        )
        task = next(item for item in protocol["tasks"] if item["task"] == task_name)
        return {**task, "role": "smoke"}
    raise ValueError(f"unknown ENGINE_SELECTION_R1 task: {task_name}")


def _source_path(task_name: str) -> Path:
    root = M4_UPSTREAM_ROOT if task_name == SMOKE_TASK else UPSTREAM_ROOT
    return root / task_name / f"{task_name}.py"


def _conditions_for_task(task_name: str) -> dict[str, Any]:
    conditions = dict(load_protocol()["common_conditions"])
    if task_name == SMOKE_TASK:
        conditions["observed_token_ceiling"] = int(
            load_protocol()["mechanics_smoke"]["observed_token_ceiling"]
        )
    return conditions


def _task_spec(task: dict[str, Any]) -> OfficialTaskSpec:
    return OfficialTaskSpec(
        name=str(task["task"]),
        class_name=str(task["class"]),
        problem_size=int(task["problem_size"]),
        source_path=str(_source_path(str(task["task"]))),
    )


def run_remote_evaluator(
    *, task_name: str, candidate: Path, seeds_path: Path, repeats: int,
    workers: int, cold: bool, output: Path,
) -> dict[str, Any]:
    task = _task_payload(task_name)
    seeds = [int(value) for value in json.loads(seeds_path.read_text())["seeds"]]
    evaluator = evaluate_official_candidate_cold if cold else evaluate_official_candidate
    kwargs: dict[str, Any] = {"repeats": repeats, "workers": workers}
    if cold:
        kwargs["timeout_seconds"] = 60.0
    result = evaluator(candidate, _task_spec(task), seeds, **kwargs)
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
    candidate: Path, spec: OfficialTaskSpec, seeds: list[int], *, repeats: int,
    workers: int, cold: bool, context: str,
) -> dict[str, Any]:
    repository_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    key = hashlib.sha256(json.dumps({
        "repository_commit": repository_commit,
        "protocol": sha256_file(PROTOCOL),
        "task": spec.name,
        "candidate": sha256_file(candidate),
        "seeds": seeds,
        "repeats": repeats,
        "workers": workers,
        "cold": cold,
        "context": context,
    }, sort_keys=True).encode()).hexdigest()[:32]
    job_dir = DEFAULT_RUN_ROOT / "remote_jobs" / key
    job_dir.mkdir(parents=True, exist_ok=True)
    staged_candidate = job_dir / "candidate.py"
    staged_seeds = job_dir / "seeds.json"
    if not staged_candidate.exists():
        shutil.copy2(candidate, staged_candidate)
    elif sha256_file(staged_candidate) != sha256_file(candidate):
        raise ValueError("remote candidate key collision")
    seed_payload = {"seeds": seeds}
    if not staged_seeds.exists():
        blind._write_json(staged_seeds, seed_payload)
    elif json.loads(staged_seeds.read_text()) != seed_payload:
        raise ValueError("remote seed key collision")

    request_path = job_dir / "request.json"
    result_dir = job_dir / "result"
    output_relative = (job_dir / "output.json").relative_to(REPO_ROOT).as_posix()
    if not request_path.exists():
        create_job_request(
            repo=REPO_ROOT,
            output=request_path,
            job_id=f"engine-r1-{key}",
            entrypoint=RemoteEntrypoint.PYTHON_MODULE,
            argv=(
                "evidence_evolve.benchmarks.engine_selection_r1_runner",
                "remote-evaluate", "--task", spec.name,
                "--candidate", staged_candidate.relative_to(REPO_ROOT).as_posix(),
                "--seeds", staged_seeds.relative_to(REPO_ROOT).as_posix(),
                "--repeats", str(repeats), "--workers", str(workers),
                *(("--cold",) if cold else ()), "--output", output_relative,
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
    receipt = (
        verify_result(request_path, result_dir)
        if result_dir.exists()
        else dispatch_job(
            repo=REPO_ROOT,
            request_path=request_path,
            host=os.environ.get("EE_REMOTE_HOST", REMOTE_HOST),
            port=int(os.environ.get("EE_REMOTE_PORT", str(REMOTE_PORT))),
            remote_root=os.environ.get("EE_REMOTE_ROOT", REMOTE_ROOT),
            local_result_dir=result_dir,
        )
    )
    if receipt.receipt.state != "SUCCEEDED":
        raise RuntimeError(f"remote Engine Selection evaluation failed: {receipt.receipt.state}")
    payload = json.loads((result_dir / "artifacts" / output_relative).read_text())
    payload["remote_receipt_sha256"] = receipt.receipt_sha256
    payload["remote_receipt_path"] = str((result_dir / "receipt.json").resolve())
    return payload


def remote_development_evaluate(
    candidate: str | Path, spec: OfficialTaskSpec, *, workers: int | None = None
) -> dict[str, Any]:
    conditions = _conditions_for_task(spec.name)
    start = int(os.environ.get("EE_ALGOTUNE_DEV_START", "0"))
    count = int(os.environ.get("EE_ALGOTUNE_DEV_COUNT", "20"))
    raw = _remote_evaluate(
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
    protocol = load_protocol()
    task = _task_payload(task_name)
    conditions = _conditions_for_task(task_name)
    seed_count = int(conditions["development_seeds_per_repeat"])
    os.environ.update({
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "EE_ALGOTUNE_DEV_START": str((repeat - 1) * seed_count),
        "EE_ALGOTUNE_DEV_COUNT": str(seed_count),
        "EE_ALGOTUNE_DEV_REPEATS": str(conditions["development_repeats"]),
        "EE_ALGOTUNE_WORKERS": str(conditions["evaluator_workers_per_active_run"]),
        "EE_HETERO_GENERATIONS": str(conditions["max_search_iterations"]),
        "EE_M4_REMOTE_EVALUATOR": "1",
        "EE_ALGOTUNE_REMOTE_MODULE": "engine_selection_r1",
        "EE_SEARCH_TOKEN_LAUNCH_CEILING": str(conditions["observed_token_ceiling"]),
        "EE_EVOX_PROTOCOL_NATIVE": "1",
        "EE_ENGINE_EVAL_CONTEXT": f"{task_name}-r{repeat}",
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
    blind.GENERATIONS = int(conditions["max_search_iterations"])
    blind.TOKEN_CEILING = int(conditions["observed_token_ceiling"])
    blind.WALL_CEILING_SECONDS = float(conditions["wall_seconds"])
    blind.EVALUATOR_WORKERS = int(conditions["evaluator_workers_per_active_run"])
    return spec


def _manifest(run_dir: Path, task_name: str, repeat: int) -> None:
    payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL),
        "task": task_name, "repeat": repeat, "arms": list(ARMS),
        "token_checkpoints": list(CHECKPOINTS),
        "stage": "MECHANICS_SMOKE" if task_name == SMOKE_TASK else _task_payload(task_name)["role"].upper(),
        "conditions": _conditions_for_task(task_name),
        "claim_scope": load_protocol()["claim_scope"],
    }
    path = run_dir / "engine_selection_manifest.json"
    if path.exists():
        existing = json.loads(path.read_text())
        for key in (
            "protocol_sha256", "task", "repeat", "arms", "token_checkpoints",
            "stage", "conditions", "claim_scope",
        ):
            if existing.get(key) != payload[key]:
                raise ValueError(f"Engine Selection manifest drift: {key}")
        return
    create_once_json(path, payload)


def _write_checkpoint(
    arm_dir: Path, budget: int, candidate: dict[str, Any], *, valid_count: int,
    attempted_count: int,
) -> dict[str, Any]:
    target = arm_dir / "token_checkpoints" / f"t{budget:06d}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(candidate["code"]), encoding="utf-8")
    return {
        "token_budget": budget,
        "candidate_path": str(target.resolve()),
        "candidate_sha256": sha256_file(target),
        "candidate_id": str(candidate["id"]),
        "candidate_cumulative_tokens": int(candidate["tokens"]),
        "development_raw_speedup": float(candidate["score"]),
        "proposal_valid_rate": valid_count / attempted_count if attempted_count else 0.0,
    }


def _select_checkpoints(
    arm_dir: Path, candidates: list[dict[str, Any]], attempted: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records = []
    for budget in CHECKPOINTS:
        eligible = [candidate for candidate in candidates if int(candidate["tokens"]) <= budget]
        if not eligible:
            raise ValueError(f"no seed candidate for {arm_dir}")
        selected = max(eligible, key=lambda item: (float(item["score"]), -int(item["tokens"])))
        attempted_at_budget = [item for item in attempted if int(item["tokens"]) <= budget]
        records.append(_write_checkpoint(
            arm_dir, budget, selected,
            valid_count=sum(bool(item["valid"]) for item in attempted_at_budget),
            attempted_count=len(attempted_at_budget),
        ))
    return records


def _extract_vanilla(arm_dir: Path) -> list[dict[str, Any]]:
    history = json.loads((arm_dir / "history.json").read_text())
    event_paths = sorted((arm_dir / "calls").glob("*.events.jsonl"))
    cumulative = [scaling._events_usage(event_paths[:index]) for index in range(1, len(event_paths) + 1)]
    seed = arm_dir / "candidates/seed.py"
    candidates = [{"id": "SEED", "code": seed.read_text(), "score": 1.0, "tokens": 0}]
    attempted = []
    for row, tokens in zip(history, cumulative):
        valid = bool(row["metrics"]["controls"].get("candidate_valid"))
        attempted.append({"tokens": tokens, "valid": valid})
        if valid:
            generation = int(row["iteration"])
            path = arm_dir / "candidates" / f"candidate_{generation:03d}.py"
            candidates.append({
                "id": f"VAN-{generation:03d}", "code": path.read_text(),
                "score": float(row["metrics"]["metrics"]["raw_speedup"]), "tokens": tokens,
            })
    return _select_checkpoints(arm_dir, candidates, attempted)


def _extract_sky(arm_dir: Path) -> list[dict[str, Any]]:
    candidates = [{
        "id": "SEED", "code": blind.INITIAL.read_text(),
        "score": float(blind.evaluate_development(blind.INITIAL)["metrics"]["raw_speedup"]),
        "tokens": 0,
    }]
    attempted: list[dict[str, Any]] = []
    seen_programs: set[str] = set()
    for checkpoint in sorted((arm_dir / "upstream/checkpoints").glob("checkpoint_*")):
        info_path = checkpoint / "best_program_info.json"
        if not info_path.exists():
            continue
        info = json.loads(info_path.read_text())
        cutoff = float(info.get("saved_at", checkpoint.stat().st_mtime))
        tokens = scaling._sky_usage_until(arm_dir, cutoff)
        programs = [json.loads(path.read_text()) for path in checkpoint.glob("programs/*.json")]
        new = [
            item for item in programs
            if int(item.get("iteration_found", 0)) > 0
            and str(item.get("id")) not in seen_programs
        ]
        seen_programs.update(str(item.get("id")) for item in new)
        attempted.extend({"tokens": tokens, "valid": bool(item.get("metrics", {}).get("correct"))} for item in new)
        candidates.append({
            "id": str(info["id"]), "code": (checkpoint / "best_program.py").read_text(),
            "score": float(info["metrics"]["raw_speedup"]), "tokens": tokens,
        })
    return _select_checkpoints(arm_dir, candidates, attempted)


def _extract_shinka(arm_dir: Path) -> list[dict[str, Any]]:
    lines = (arm_dir / "headless_usage.jsonl").read_text().splitlines()
    cumulative = [scaling._headless_usage_lines(lines[:index]) for index in range(1, len(lines) + 1)]
    connection = sqlite3.connect(arm_dir / "upstream/programs.sqlite")
    try:
        rows = connection.execute(
            "SELECT id, code, generation, combined_score, correct FROM programs ORDER BY generation, timestamp"
        ).fetchall()
    finally:
        connection.close()
    candidates: list[dict[str, Any]] = []
    attempted: list[dict[str, Any]] = []
    for row in rows:
        generation = int(row[2])
        tokens = cumulative[min(generation, len(cumulative)) - 1] if generation > 0 and cumulative else 0
        if generation > 0:
            attempted.append({"tokens": tokens, "valid": bool(row[4])})
        if bool(row[4]):
            candidates.append({"id": str(row[0]), "code": str(row[1]), "score": float(row[3]), "tokens": tokens})
    return _select_checkpoints(arm_dir, candidates, attempted)


def run_arm(run_root: Path, task_name: str, repeat: int, arm: str) -> dict[str, Any]:
    if arm not in ARMS or repeat not in (1, 2):
        raise ValueError("invalid Engine Selection arm or repeat")
    run_dir = run_root / task_name / f"repeat_{repeat:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _configure(run_dir, task_name, repeat)
    _manifest(run_dir, task_name, repeat)
    arm_dir = run_dir / "arms" / arm
    trajectory_path = arm_dir / "trajectory_result.json"
    if trajectory_path.exists():
        return json.loads(trajectory_path.read_text())
    started = time.perf_counter()
    if arm == "vanilla":
        arm_result = blind.run_vanilla(run_dir)
        checkpoints = _extract_vanilla(arm_dir)
    elif arm == "ada":
        arm_result = blind.run_ada(run_dir)
        checkpoints = _extract_sky(arm_dir)
    elif arm == "evox":
        arm_result = blind.run_evox(run_dir)
        checkpoints = _extract_sky(arm_dir)
    else:
        arm_result = blind.run_shinka(run_dir)
        checkpoints = _extract_shinka(arm_dir)
    conditions = _conditions_for_task(task_name)
    tokens = int(arm_result["tokens"])
    wall = float(arm_result["wall_seconds"])
    payload = {
        "schema_version": "1.0", "task": task_name, "repeat": repeat,
        "arm": arm, "model": blind.MODEL, "reasoning_effort": blind.REASONING_EFFORT,
        "continuous_trajectory": True,
        "run_valid": tokens <= int(conditions["observed_token_ceiling"]) and wall <= float(conditions["wall_seconds"]),
        "observed_tokens": tokens, "wall_seconds": wall,
        "checkpoints": checkpoints, "final_arm_result": arm_result,
        "runner_elapsed_seconds": time.perf_counter() - started,
    }
    blind._write_json(trajectory_path, payload)
    return payload


def _core_items(run_root: Path) -> list[tuple[str, int, str, list[str]]]:
    return [
        (task["task"], repeat, arm, [
            sys.executable, "-m", "evidence_evolve.benchmarks.engine_selection_r1_runner",
            "arm", "--run-root", str(run_root), "--task", task["task"],
            "--repeat", str(repeat), "--arm", arm,
        ])
        for task in load_protocol()["tasks"] if task["role"] == "core"
        for repeat in (1, 2) for arm in ARMS
    ]


def _run_item(item: tuple[str, int, str, list[str]], run_root: Path) -> dict[str, Any]:
    task, repeat, arm, command = item
    process_dir = run_root / task / f"repeat_{repeat:02d}" / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status = process_dir / f"{arm}.json"
    if status.exists():
        return json.loads(status.read_text())
    stdout = process_dir / f"{arm}.stdout.log"
    stderr = process_dir / f"{arm}.stderr.log"
    started = time.monotonic()
    returncode: int | None = None
    state = "FAILED"
    try:
        with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
            completed = subprocess.run(
                command, cwd=REPO_ROOT, check=False, stdout=out, stderr=err,
                timeout=float(load_protocol()["common_conditions"]["wall_seconds"]) + 120,
                env={**os.environ, "PYTHONUTF8": "1"},
            )
        returncode = completed.returncode
        state = "SUCCEEDED" if returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired:
        state = "TIMED_OUT"
    payload = {
        "task": task, "repeat": repeat, "arm": arm,
        "state": state, "returncode": returncode,
        "elapsed_seconds": time.monotonic() - started,
        "stdout_sha256": sha256_file(stdout), "stderr_sha256": sha256_file(stderr),
    }
    blind._write_json(status, payload)
    return payload


def search_core(run_root: Path, max_parallel: int) -> list[dict[str, Any]]:
    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")
    smoke_path = run_root / "mechanics_smoke_receipt.json"
    if not smoke_path.exists():
        raise ValueError("passing mechanics smoke required before formal core search")
    smoke = json.loads(smoke_path.read_text())
    if (
        smoke.get("status") != "PASS"
        or smoke.get("protocol_sha256") != sha256_file(PROTOCOL)
        or smoke.get("scientific_authority") is not False
    ):
        raise ValueError("mechanics smoke did not admit formal core search")
    for task in [item["task"] for item in load_protocol()["tasks"] if item["role"] == "core"]:
        for repeat in (1, 2):
            run_dir = run_root / task / f"repeat_{repeat:02d}"
            run_dir.mkdir(parents=True, exist_ok=True)
            _configure(run_dir, task, repeat)
            _manifest(run_dir, task, repeat)
    results = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_run_item, item, run_root) for item in _core_items(run_root)]
        for future in as_completed(futures):
            results.append(future.result())
    blind._write_json(run_root / "core_search_process_summary.json", results)
    return results


def run_mechanics_smoke(run_root: Path, max_parallel: int) -> dict[str, Any]:
    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")
    run_dir = run_root / SMOKE_TASK / "repeat_01"
    run_dir.mkdir(parents=True, exist_ok=True)
    _configure(run_dir, SMOKE_TASK, 1)
    _manifest(run_dir, SMOKE_TASK, 1)
    items = [
        (SMOKE_TASK, 1, arm, [
            sys.executable, "-m", "evidence_evolve.benchmarks.engine_selection_r1_runner",
            "arm", "--run-root", str(run_root), "--task", SMOKE_TASK,
            "--repeat", "1", "--arm", arm,
        ])
        for arm in ARMS
    ]
    statuses = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_run_item, item, run_root) for item in items]
        for future in as_completed(futures):
            statuses.append(future.result())
    trajectories = []
    for arm in ARMS:
        path = run_dir / "arms" / arm / "trajectory_result.json"
        if path.exists():
            payload = json.loads(path.read_text())
            for checkpoint in payload["checkpoints"]:
                candidate = Path(checkpoint["candidate_path"])
                if sha256_file(candidate) != checkpoint["candidate_sha256"]:
                    raise ValueError("smoke candidate drift")
            trajectories.append(payload)
    passed = (
        len(statuses) == 4
        and all(item["state"] == "SUCCEEDED" for item in statuses)
        and len(trajectories) == 4
        and all(item["run_valid"] for item in trajectories)
        and all(int(item["observed_tokens"]) > 0 for item in trajectories)
    )
    payload = {
        "schema_version": "1.0", "campaign": CAMPAIGN,
        "stage": "MECHANICS_SMOKE", "scientific_authority": False,
        "protocol_sha256": sha256_file(PROTOCOL),
        "task": SMOKE_TASK, "consumed_task_only": True,
        "status": "PASS" if passed else "FAIL",
        "formal_search_admitted": passed,
        "statuses": sorted(statuses, key=lambda item: item["arm"]),
        "arms": [{
            "arm": item["arm"], "run_valid": item["run_valid"],
            "observed_tokens": item["observed_tokens"],
            "wall_seconds": item["wall_seconds"],
        } for item in sorted(trajectories, key=lambda item: item["arm"])],
    }
    blind._write_json(run_root / "mechanics_smoke_receipt.json", payload)
    return payload


def _reserve_items(
    run_root: Path, participants: list[str]
) -> list[tuple[str, int, str, list[str]]]:
    task = next(item["task"] for item in load_protocol()["tasks"] if item["role"] == "reserve")
    return [
        (task, repeat, arm, [
            sys.executable, "-m", "evidence_evolve.benchmarks.engine_selection_r1_runner",
            "arm", "--run-root", str(run_root), "--task", task,
            "--repeat", str(repeat), "--arm", arm,
        ])
        for repeat in (1, 2) for arm in participants
    ]


def search_reserve(run_root: Path, max_parallel: int) -> list[dict[str, Any]]:
    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")
    core_result_path = run_root / "core_result.json"
    if not core_result_path.exists():
        raise ValueError("core result required before reserve search")
    core_result = json.loads(core_result_path.read_text())
    if not bool(core_result.get("reserve_required")):
        raise ValueError("core result did not trigger reserve")
    participants = [str(arm) for arm in core_result["reserve_participants"]]
    task = next(item["task"] for item in load_protocol()["tasks"] if item["role"] == "reserve")
    for repeat in (1, 2):
        run_dir = run_root / task / f"repeat_{repeat:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        _configure(run_dir, task, repeat)
        _manifest(run_dir, task, repeat)
    results = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [
            pool.submit(_run_item, item, run_root)
            for item in _reserve_items(run_root, participants)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    blind._write_json(run_root / "reserve_search_process_summary.json", results)
    return results


def lock_core(run_root: Path) -> dict[str, Any]:
    if any(run_root.rglob("heldout_seeds.json")):
        raise ValueError("heldout seeds existed before Engine Selection core lock")
    entries = []
    for task in [item["task"] for item in load_protocol()["tasks"] if item["role"] == "core"]:
        for repeat in (1, 2):
            for arm in ARMS:
                path = run_root / task / f"repeat_{repeat:02d}" / "arms" / arm / "trajectory_result.json"
                if not path.exists():
                    raise ValueError(f"missing core trajectory: {task}:{repeat}:{arm}")
                trajectory = json.loads(path.read_text())
                checkpoints = []
                for checkpoint in trajectory["checkpoints"]:
                    candidate = Path(checkpoint["candidate_path"])
                    if sha256_file(candidate) != checkpoint["candidate_sha256"]:
                        raise ValueError("checkpoint candidate drift")
                    checkpoints.append({key: checkpoint[key] for key in (
                        "token_budget", "candidate_path", "candidate_sha256", "candidate_cumulative_tokens"
                    )})
                entries.append({
                    "task": task, "repeat": repeat, "arm": arm,
                    "trajectory_sha256": sha256_file(path), "run_valid": trajectory["run_valid"],
                    "checkpoints": checkpoints,
                })
    payload = {
        "schema_version": "1.0", "locked_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL), "all_24_core_trajectories_locked": len(entries) == 24,
        "all_72_checkpoint_candidates_locked": sum(len(item["checkpoints"]) for item in entries) == 72,
        "heldout_existed_at_lock": False, "entries": entries,
    }
    path = run_root / "core_candidate_lock.json"
    if path.exists():
        return json.loads(path.read_text())
    create_once_json(path, payload)
    return payload


def lock_reserve(run_root: Path) -> dict[str, Any]:
    core_result = json.loads((run_root / "core_result.json").read_text())
    if not bool(core_result.get("reserve_required")):
        raise ValueError("core result did not trigger reserve")
    participants = [str(arm) for arm in core_result["reserve_participants"]]
    task = next(item["task"] for item in load_protocol()["tasks"] if item["role"] == "reserve")
    if any((run_root / task).rglob("heldout_seeds.json")):
        raise ValueError("reserve heldout seeds existed before reserve lock")
    entries = []
    for repeat in (1, 2):
        for arm in participants:
            path = run_root / task / f"repeat_{repeat:02d}" / "arms" / arm / "trajectory_result.json"
            if not path.exists():
                raise ValueError(f"missing reserve trajectory: {task}:{repeat}:{arm}")
            trajectory = json.loads(path.read_text())
            checkpoints = []
            for checkpoint in trajectory["checkpoints"]:
                candidate = Path(checkpoint["candidate_path"])
                if sha256_file(candidate) != checkpoint["candidate_sha256"]:
                    raise ValueError("reserve checkpoint candidate drift")
                checkpoints.append({key: checkpoint[key] for key in (
                    "token_budget", "candidate_path", "candidate_sha256", "candidate_cumulative_tokens"
                )})
            entries.append({
                "task": task, "repeat": repeat, "arm": arm,
                "trajectory_sha256": sha256_file(path), "run_valid": trajectory["run_valid"],
                "checkpoints": checkpoints,
            })
    payload = {
        "schema_version": "1.0", "locked_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL), "core_result_sha256": sha256_file(run_root / "core_result.json"),
        "participants": participants, "all_4_reserve_trajectories_locked": len(entries) == 4,
        "all_12_checkpoint_candidates_locked": sum(len(item["checkpoints"]) for item in entries) == 12,
        "heldout_existed_at_lock": False, "entries": entries,
    }
    path = run_root / "reserve_candidate_lock.json"
    if path.exists():
        return json.loads(path.read_text())
    create_once_json(path, payload)
    return payload


def _heldout_seeds(run_root: Path, task: str, repeat: int) -> list[int]:
    path = run_root / task / f"repeat_{repeat:02d}" / "heldout_seeds.json"
    if path.exists():
        return [int(value) for value in json.loads(path.read_text())["seeds"]]
    role = str(_task_payload(task)["role"])
    required_lock = run_root / (
        "core_candidate_lock.json" if role == "core" else "reserve_candidate_lock.json"
    )
    if not required_lock.exists():
        raise ValueError(f"{role} lock required before heldout seed generation")
    count = int(load_protocol()["common_conditions"]["heldout_seeds_per_repeat"])
    excluded = set(range(60))
    values: set[int] = set()
    while len(values) < count:
        value = secrets.randbelow(2**32)
        if value not in excluded:
            values.add(value)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_after_core_lock": True,
        "core_lock_sha256": sha256_file(run_root / "core_candidate_lock.json"),
        "generator": "Python secrets.SystemRandom / OS CSPRNG", "seeds": sorted(values),
    }
    create_once_json(path, payload)
    return payload["seeds"]


def _evaluate_block(run_root: Path, task: str, repeat: int, arm: str) -> dict[str, Any]:
    run_dir = run_root / task / f"repeat_{repeat:02d}"
    result_path = run_dir / "heldout" / f"{arm}.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    trajectory = json.loads((run_dir / "arms" / arm / "trajectory_result.json").read_text())
    evaluations = []
    for checkpoint in trajectory["checkpoints"]:
        candidate = Path(checkpoint["candidate_path"])
        if sha256_file(candidate) != checkpoint["candidate_sha256"]:
            raise ValueError("candidate drift before heldout")
        heldout = _remote_evaluate(
            candidate, _task_spec(_task_payload(task)), _heldout_seeds(run_root, task, repeat),
            repeats=int(load_protocol()["common_conditions"]["heldout_repeats"]),
            workers=int(load_protocol()["common_conditions"]["evaluator_workers_per_active_run"]),
            cold=True, context=f"{task}-r{repeat}-{arm}-heldout-t{checkpoint['token_budget']}",
        )
        evaluations.append({**checkpoint, "heldout": heldout})
    payload = {
        "task": task, "repeat": repeat, "arm": arm,
        "run_valid": bool(trajectory["run_valid"]),
        "observed_tokens": int(trajectory["observed_tokens"]),
        "wall_seconds": float(trajectory["wall_seconds"]),
        "proposal_valid_rate": float(trajectory["final_arm_result"]["proposal_valid_rate"]),
        "checkpoints": evaluations,
        "authority": "ENGINE_SELECTION_R1_HELDOUT_DECISION_EVIDENCE_ONLY",
    }
    blind._write_json(result_path, payload)
    return payload


def finalize_core(run_root: Path, max_parallel: int) -> dict[str, Any]:
    lock_core(run_root)
    tasks = [item["task"] for item in load_protocol()["tasks"] if item["role"] == "core"]
    for task in tasks:
        for repeat in (1, 2):
            _heldout_seeds(run_root, task, repeat)
    work = [(task, repeat, arm) for task in tasks for repeat in (1, 2) for arm in ARMS]
    blocks = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_evaluate_block, run_root, *item) for item in work]
        for future in as_completed(futures):
            blocks.append(future.result())
    blocks.sort(key=lambda item: (item["task"], item["repeat"], item["arm"]))
    result = score_core(blocks)
    blind._write_json(run_root / "core_blocks.json", blocks)
    blind._write_json(run_root / "core_result.json", result)
    return result


def finalize_reserve(run_root: Path, max_parallel: int) -> dict[str, Any]:
    lock_reserve(run_root)
    core_result = json.loads((run_root / "core_result.json").read_text())
    participants = [str(arm) for arm in core_result["reserve_participants"]]
    task = next(item["task"] for item in load_protocol()["tasks"] if item["role"] == "reserve")
    for repeat in (1, 2):
        _heldout_seeds(run_root, task, repeat)
    work = [(task, repeat, arm) for repeat in (1, 2) for arm in participants]
    blocks = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_evaluate_block, run_root, *item) for item in work]
        for future in as_completed(futures):
            blocks.append(future.result())
    blocks.sort(key=lambda item: (item["task"], item["repeat"], item["arm"]))
    result = score_reserve(core_result, blocks)
    blind._write_json(run_root / "reserve_blocks.json", blocks)
    blind._write_json(run_root / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="ENGINE_SELECTION_R1 execution harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    arm = subparsers.add_parser("arm")
    arm.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    arm.add_argument("--task", required=True)
    arm.add_argument("--repeat", type=int, choices=(1, 2), required=True)
    arm.add_argument("--arm", choices=ARMS, required=True)
    search = subparsers.add_parser("search-core")
    search.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    search.add_argument("--max-parallel", type=int, default=4)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    smoke.add_argument("--max-parallel", type=int, default=4)
    finish = subparsers.add_parser("finalize-core")
    finish.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    finish.add_argument("--max-parallel", type=int, default=4)
    reserve = subparsers.add_parser("search-reserve")
    reserve.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    reserve.add_argument("--max-parallel", type=int, default=4)
    finish_reserve = subparsers.add_parser("finalize-reserve")
    finish_reserve.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    finish_reserve.add_argument("--max-parallel", type=int, default=4)
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
            task_name=args.task, candidate=Path(args.candidate), seeds_path=Path(args.seeds),
            repeats=args.repeats, workers=args.workers, cold=args.cold, output=Path(args.output),
        )
    elif args.command == "arm":
        result = run_arm(args.run_root.resolve(), args.task, args.repeat, args.arm)
    elif args.command == "search-core":
        result = search_core(args.run_root.resolve(), args.max_parallel)
    elif args.command == "smoke":
        result = run_mechanics_smoke(args.run_root.resolve(), args.max_parallel)
    elif args.command == "finalize-core":
        result = finalize_core(args.run_root.resolve(), args.max_parallel)
    elif args.command == "search-reserve":
        result = search_reserve(args.run_root.resolve(), args.max_parallel)
    else:
        result = finalize_reserve(args.run_root.resolve(), args.max_parallel)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
