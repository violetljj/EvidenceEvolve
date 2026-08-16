from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_evolve.artifacts import ReceiptAlreadyExistsError, create_once_json
from evidence_evolve.benchmarks import algotune_blind as blind
from evidence_evolve.benchmarks import algotune_heterogeneous as heterogeneous
from evidence_evolve.hashing import sha256_bytes, sha256_file
from tasks.algotune_set_cover.autonomous_adapter import (
    evaluate_candidate as set_cover_evaluate_ee,
)
from tasks.algotune_set_cover.campaign_evaluator import (
    evaluate_development as set_cover_development,
)
from tasks.algotune_set_cover.common import evaluate_candidate as set_cover_heldout


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "research/parity/algotune_horizon_scaling_v0.protocol.json"
HORIZONS = (3, 6, 12, 24, 50)
TASKS = (
    "set_cover",
    "articulation_points",
    "job_shop_scheduling",
    "stable_matching",
    "cumulative_simpson_1d",
)
ARMS = ("shinka", "ada", "evox", "evidence_evolve")
GENERATION_RE = re.compile(r"GEN-(\d{3})")


def _set_cover_workspace(run_dir: Path) -> None:
    source = REPO_ROOT / "tasks/algotune_set_cover"
    task_root = run_dir / "task"
    if not task_root.exists():
        shutil.copytree(source, task_root)
    blind.TASK_ROOT = task_root
    blind.INITIAL = task_root / "initial.py"
    blind.TASK_PROMPT = (
        "Optimize the exact set-cover solver below for runtime on deterministic "
        "development inputs. Preserve class Solver and solve(problem), return an exact "
        "minimum-cardinality 1-based cover for every input."
    )
    blind.CONTRACT_TEMPLATE = (
        REPO_ROOT / "research/contracts/algotune_set_cover_blind_v0.template.yaml"
    )
    blind.POLICY_PATH = REPO_ROOT / "research/policies/algotune_set_cover_blind_v0.yaml"
    blind.CANDIDATE_RELATIVE = Path("tasks/algotune_set_cover/initial.py")
    blind.CAMPAIGN_ID = "algotune-set-cover-horizon-scaling"
    blind.DEV_COUNT = 100
    blind.DEV_REPEATS = 3
    blind.EVALUATOR_WORKERS = 3
    blind.evaluate_development = set_cover_development
    blind.evaluate_ee = set_cover_evaluate_ee
    blind.evaluate_candidate = set_cover_heldout


def configure_task(run_dir: Path, task_name: str) -> dict[str, Any] | None:
    blind.GENERATIONS = 50
    if task_name == "set_cover":
        _set_cover_workspace(run_dir)
        return None
    task = heterogeneous._task_payload(task_name)
    heterogeneous._configure(run_dir, task)
    blind.GENERATIONS = 50
    return task


def _manifest(run_dir: Path, task_name: str) -> None:
    payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256_file(PROTOCOL),
        "task": task_name,
        "arms": list(ARMS),
        "horizons": list(HORIZONS),
        "model": blind.MODEL,
        "reasoning_effort": blind.REASONING_EFFORT,
        "trajectory_design": "one continuous 50-generation run per task-arm",
    }
    stable_keys = (
        "protocol_sha256",
        "task",
        "arms",
        "horizons",
        "model",
        "reasoning_effort",
        "trajectory_design",
    )
    # SkyDiscover's EvoX adapter still hashes the legacy manifest filename.
    # Keep both immutable files so the compatibility detail cannot abort an arm.
    for path in (run_dir / "scaling_manifest.json", run_dir / "manifest.json"):
        try:
            create_once_json(path, payload)
        except ReceiptAlreadyExistsError:
            existing = json.loads(path.read_text(encoding="utf-8"))
            for key in stable_keys:
                if existing.get(key) != payload[key]:
                    raise ValueError(f"scaling manifest drift in {path.name}: {key}")


def _events_usage(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict):
                total += int(usage.get("input_tokens", 0) or 0)
                total += int(usage.get("output_tokens", 0) or 0)
    return total


def _headless_usage_lines(lines: list[str]) -> int:
    total = 0
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            total += int(usage.get("inputTokens", 0) or 0)
            total += int(usage.get("cacheReadTokens", 0) or 0)
            total += int(usage.get("outputTokens", 0) or 0)
    return total


def _sky_usage_until(arm_dir: Path, cutoff: float) -> int:
    paths = [
        path
        for path in arm_dir.glob("calls/*.events.jsonl")
        if path.stat().st_mtime <= cutoff + 0.001
    ]
    return _events_usage(paths)


def _generation_from_path(path: Path) -> int | None:
    match = GENERATION_RE.search(str(path))
    return int(match.group(1)) if match else None


def _ee_usage_until(arm_dir: Path, horizon: int) -> int:
    return _events_usage(
        [
            path
            for path in arm_dir.rglob("*.events.jsonl")
            if (generation := _generation_from_path(path)) is not None
            and generation <= horizon
        ]
    )


def _copy_checkpoint(
    arm_dir: Path,
    horizon: int,
    code: str,
    *,
    selected_id: str,
    selected_generation: int,
    dev_score: float,
    tokens: int,
    wall_seconds: float,
    proposal_valid_rate: float,
) -> dict[str, Any]:
    target = arm_dir / "horizon_checkpoints" / f"h{horizon:03d}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code, encoding="utf-8")
    return {
        "horizon": horizon,
        "candidate_path": str(target.resolve()),
        "candidate_sha256": sha256_file(target),
        "selected_id": selected_id,
        "selected_generation": selected_generation,
        "development_raw_speedup": dev_score,
        "cumulative_tokens": tokens,
        "cumulative_wall_seconds": wall_seconds,
        "proposal_valid_rate": proposal_valid_rate,
    }


def _extract_sky(
    arm_dir: Path, arm_result: dict[str, Any], started_epoch: float
) -> list[dict[str, Any]]:
    root = arm_dir / "upstream/checkpoints"
    available = sorted(
        int(path.name.split("_")[-1])
        for path in root.glob("checkpoint_*")
        if path.name.split("_")[-1].isdigit()
    )
    records: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        eligible = [iteration for iteration in available if iteration <= horizon]
        if not eligible:
            raise ValueError(f"no {arm_result['arm']} checkpoint at horizon {horizon}")
        iteration = max(eligible)
        checkpoint = root / f"checkpoint_{iteration}"
        info = json.loads((checkpoint / "best_program_info.json").read_text())
        code = (checkpoint / "best_program.py").read_text(encoding="utf-8")
        programs: dict[str, dict[str, Any]] = {}
        for path in checkpoint.glob("programs/*.json"):
            payload = json.loads(path.read_text())
            programs[str(payload.get("id"))] = payload
        proposals = [
            item
            for item in programs.values()
            if 0 < int(item.get("iteration_found", 0)) <= horizon
        ]
        valid = sum(int(bool(item.get("metrics", {}).get("correct"))) for item in proposals)
        cutoff = float(info.get("saved_at", checkpoint.stat().st_mtime))
        records.append(
            _copy_checkpoint(
                arm_dir,
                horizon,
                code,
                selected_id=str(info["id"]),
                selected_generation=int(info.get("generation", iteration)),
                dev_score=float(info["metrics"]["raw_speedup"]),
                tokens=(
                    int(arm_result["tokens"])
                    if horizon == HORIZONS[-1]
                    else _sky_usage_until(arm_dir, cutoff)
                ),
                wall_seconds=(
                    float(arm_result["wall_seconds"])
                    if horizon == HORIZONS[-1]
                    else max(cutoff - started_epoch, 0.0)
                ),
                proposal_valid_rate=valid / len(proposals) if proposals else 0.0,
            )
        )
    return records


def _extract_shinka(
    arm_dir: Path, arm_result: dict[str, Any], started_epoch: float
) -> list[dict[str, Any]]:
    database = arm_dir / "upstream/programs.sqlite"
    usage_lines = (arm_dir / "headless_usage.jsonl").read_text().splitlines()
    connection = sqlite3.connect(database)
    records: list[dict[str, Any]] = []
    try:
        for horizon in HORIZONS:
            row = connection.execute(
                "SELECT id, code, generation, combined_score FROM programs "
                "WHERE correct = 1 AND generation <= ? "
                "ORDER BY combined_score DESC, timestamp ASC LIMIT 1",
                (horizon,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no Shinka checkpoint at horizon {horizon}")
            counts = connection.execute(
                "SELECT COUNT(*), SUM(CASE WHEN correct THEN 1 ELSE 0 END), "
                "MAX(timestamp) FROM programs WHERE generation BETWEEN 1 AND ?",
                (horizon,),
            ).fetchone()
            tokens = _headless_usage_lines(usage_lines[:horizon])
            records.append(
                _copy_checkpoint(
                    arm_dir,
                    horizon,
                    str(row[1]),
                    selected_id=str(row[0]),
                    selected_generation=int(row[2]),
                    dev_score=float(row[3]),
                    tokens=(int(arm_result["tokens"]) if horizon == 50 else tokens),
                    wall_seconds=(
                        float(arm_result["wall_seconds"])
                        if horizon == 50
                        else max(float(counts[2] or started_epoch) - started_epoch, 0.0)
                    ),
                    proposal_valid_rate=float(counts[1] or 0) / float(counts[0] or 1),
                )
            )
    finally:
        connection.close()
    return records


def _git_show(repo: Path, commit: str, relative: Path) -> str:
    return subprocess.run(
        ["git", "show", f"{commit}:{relative.as_posix()}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _extract_evidence(
    run_dir: Path,
    task_name: str,
    arm_result: dict[str, Any],
    started_epoch: float,
) -> list[dict[str, Any]]:
    arm_dir = run_dir / "arms/evidence_evolve"
    campaign = arm_dir / "campaign"
    execution_repo = run_dir / "execution_repo"
    relative = (
        Path("tasks/algotune_set_cover/initial.py")
        if task_name == "set_cover"
        else Path(f"tasks/algotune_portfolio/{task_name}/initial.py")
    )
    baseline_path = execution_repo / relative
    baseline = blind.evaluate_development(baseline_path)
    candidates: list[dict[str, Any]] = [
        {
            "id": "SEED",
            "generation": 0,
            "score": float(baseline["metrics"]["raw_speedup"]),
            "code": baseline_path.read_text(encoding="utf-8"),
        }
    ]
    for receipt_path in campaign.glob("candidates/GEN-*/receipts/*.json"):
        envelope = json.loads(receipt_path.read_text())
        if "receipt" not in envelope:
            continue
        receipt = envelope["receipt"]
        candidate_id = str(receipt["candidate_id"])
        generation = _generation_from_path(Path(candidate_id))
        evaluation = receipt["evaluation_input"]
        commit = receipt.get("candidate_commit")
        if (
            generation is None
            or not commit
            or not evaluation["controls"].get("candidate_valid")
        ):
            continue
        candidates.append(
            {
                "id": candidate_id,
                "generation": generation,
                "score": float(evaluation["metrics"]["raw_speedup"]),
                "code": _git_show(execution_repo, str(commit), relative),
            }
        )
    records: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        eligible = [item for item in candidates if item["generation"] <= horizon]
        selected = max(eligible, key=lambda item: item["score"])
        files = [
            path
            for path in campaign.rglob("*")
            if path.is_file()
            and (generation := _generation_from_path(path)) is not None
            and generation <= horizon
        ]
        records.append(
            _copy_checkpoint(
                arm_dir,
                horizon,
                str(selected["code"]),
                selected_id=str(selected["id"]),
                selected_generation=int(selected["generation"]),
                dev_score=float(selected["score"]),
                tokens=(
                    int(arm_result["tokens"])
                    if horizon == 50
                    else _ee_usage_until(arm_dir, horizon)
                ),
                wall_seconds=(
                    float(arm_result["wall_seconds"])
                    if horizon == 50
                    else max(max((path.stat().st_mtime for path in files), default=started_epoch) - started_epoch, 0.0)
                ),
                proposal_valid_rate=(
                    sum(1 for item in candidates if 0 < item["generation"] <= horizon)
                    / horizon
                ),
            )
        )
    return records


def run_arm(run_dir: Path, task_name: str, arm: str) -> dict[str, Any]:
    task = configure_task(run_dir, task_name)
    _manifest(run_dir, task_name)
    arm_dir = run_dir / "arms" / arm
    trajectory_path = arm_dir / "trajectory_result.json"
    if trajectory_path.exists():
        return json.loads(trajectory_path.read_text())
    arm_result_path = arm_dir / "arm_result.json"
    if arm_result_path.exists():
        cached_result = json.loads(arm_result_path.read_text(encoding="utf-8"))
        started_epoch = arm_result_path.stat().st_mtime - float(
            cached_result.get("wall_seconds", 0.0)
        )
    else:
        started_epoch = time.time()
    if arm == "evidence_evolve":
        if task_name == "set_cover":
            arm_result = blind.run_evidence_evolve(run_dir)
        else:
            if task is None:  # pragma: no cover - guarded by configure_task
                raise ValueError("heterogeneous task payload is missing")
            arm_result = heterogeneous._run_evidence(
                run_dir, task, heterogeneous._spec(task)
            )
        checkpoints = _extract_evidence(run_dir, task_name, arm_result, started_epoch)
    elif arm == "shinka":
        arm_result = blind.run_shinka(run_dir)
        checkpoints = _extract_shinka(arm_dir, arm_result, started_epoch)
    else:
        arm_result = blind.RUNNERS[arm](run_dir)
        checkpoints = _extract_sky(arm_dir, arm_result, started_epoch)
    payload = {
        "schema_version": "1.0",
        "task": task_name,
        "arm": arm,
        "model": blind.MODEL,
        "reasoning_effort": blind.REASONING_EFFORT,
        "continuous_trajectory": True,
        "checkpoints": checkpoints,
        "final_arm_result": arm_result,
    }
    blind._write_json(trajectory_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=TASKS, required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    for key, value in {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "EE_ALGOTUNE_WORKERS": "3",
        "EE_HETERO_GENERATIONS": "50",
    }.items():
        os.environ[key] = value
    run_dir = (
        args.run_dir
        or REPO_ROOT / "runs/algotune_horizon_scaling_v0" / args.task
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    run_arm(run_dir, args.task, args.arm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
