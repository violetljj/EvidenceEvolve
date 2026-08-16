"""Prospective M2-R1B Set Cover development-only mechanism experiment.

This module intentionally exposes no held-out/finalize path. Each arm receives the
same fixed development budget and its own execution repository and worktrees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.benchmarks import algotune_blind as benchmark
from evidence_evolve.discovery.m2_escape import (
    M2AutonomousCampaignRunner,
    M2EscapeCampaignController,
    M2EscapePolicy,
)
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import ProtocolLock, dump_contract, load_contract
from evidence_evolve.hashing import sha256_file
from evidence_evolve.models import Budgets


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = REPO_ROOT / "runs/algotune_set_cover_m2_r1b_dev_v1"
PROTOCOL = (
    REPO_ROOT
    / "research/results/algotune_set_cover_m2_r1a_controller_replay_v0/protocol.json"
)
AMENDMENT = (
    REPO_ROOT
    / "research/results/algotune_set_cover_m2_r1b_escape_v0/"
    "protocol_amendment_parent_rights_v1.json"
)
HORIZON = 16
ARMS = {
    "controller_only": (
        REPO_ROOT
        / "research/policies/algotune_set_cover_m2_r1_controller_only_v0.yaml"
    ),
    "radical_roots": (
        REPO_ROOT
        / "research/policies/algotune_set_cover_m2_r1_radical_roots_v0.yaml"
    ),
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _resource_receipt() -> dict[str, Any]:
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "cpu_affinity_count": len(os.sched_getaffinity(0)),
        "cgroup_cpu_max": (
            cpu_max.read_text(encoding="utf-8").strip()
            if cpu_max.is_file()
            else None
        ),
        "system_memory_bytes": os.sysconf("SC_PAGE_SIZE")
        * os.sysconf("SC_PHYS_PAGES"),
        "visible_gpus": (
            gpu.stdout.strip().splitlines() if gpu.returncode == 0 else []
        ),
        "arm_parallelism": 2,
        "within_arm_parallelism": 1,
        "evaluator_workers_per_arm": 3,
        "reason": (
            "arms are independent and receive equal fixed quotas; each search "
            "trajectory remains serial because generation state is causal"
        ),
    }


def _manifest(run_root: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "study_id": "algotune_set_cover_m2_r1b_dev_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo_commit": _git("rev-parse", "HEAD"),
        "working_tree_included_in_execution_snapshot": True,
        "protocol_path": str(PROTOCOL.relative_to(REPO_ROOT)),
        "protocol_sha256": sha256_file(PROTOCOL),
        "protocol_amendment_path": str(AMENDMENT.relative_to(REPO_ROOT)),
        "protocol_amendment_sha256": sha256_file(AMENDMENT),
        "scope": "prospective Set Cover development-only search",
        "arms": list(ARMS),
        "horizon": HORIZON,
        "model": benchmark.MODEL,
        "reasoning_effort": benchmark.REASONING_EFFORT,
        "development_instances": 100,
        "development_repeats": 3,
        "blind_instances_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
        "resources": _resource_receipt(),
    }
    path = run_root / "manifest.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        stable = (
            "study_id",
            "protocol_sha256",
            "protocol_amendment_sha256",
            "scope",
            "arms",
            "horizon",
            "model",
            "reasoning_effort",
            "development_instances",
            "development_repeats",
            "blind_instances_read",
            "blind_evaluator_calls",
            "confirmation_runs",
        )
        for key in stable:
            if existing[key] != payload[key]:
                raise ValueError(f"M2-R1B manifest drift: {key}")
        return
    create_once_json(path, payload)


def _runtime_contract(
    execution_repo: Path,
    arm_dir: Path,
    arm: str,
) -> Any:
    template = execution_repo / benchmark.CONTRACT_TEMPLATE.relative_to(REPO_ROOT)
    contract = load_contract(template)
    contract.campaign.id = f"algotune-set-cover-m2-r1b-{arm}"
    contract.campaign.base_commit = benchmark._git(
        execution_repo, "rev-parse", "HEAD"
    )
    contract.campaign.claim_scope = "SET_COVER_M2_R1B_DEVELOPMENT_ONLY"
    contract.budgets = Budgets(
        proposal_calls=HORIZON,
        implementations=HORIZON,
        mechanics_runs=HORIZON,
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_contract(locked, arm_dir / "campaign_contract.locked.yaml")
    return locked


def run_arm(run_root: Path, arm: str) -> dict[str, Any]:
    if arm not in ARMS:
        raise ValueError(f"unknown M2-R1B arm: {arm}")
    _manifest(run_root)
    arm_dir = run_root / arm
    result_path = arm_dir / "arm_result.json"
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    started = time.perf_counter()
    execution_repo = benchmark._prepare_execution_repo(arm_dir)
    contract = _runtime_contract(execution_repo, arm_dir, arm)
    relative_policy = ARMS[arm].relative_to(REPO_ROOT)
    policy = M2EscapePolicy.model_validate(
        yaml.safe_load(
            (execution_repo / relative_policy).read_text(encoding="utf-8")
        )
    )
    campaign_dir = arm_dir / "campaign"
    worktree_root = (
        Path(tempfile.gettempdir())
        / f"ee-algotune-m2-r1b-{arm}-worktrees"
    )
    baseline = execution_repo / benchmark.CANDIDATE_RELATIVE
    baseline_metrics = benchmark.evaluate_development(baseline)["metrics"]
    runner = M2AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(
            execution_repo / contract.closure_registry
        ),
        policy=policy.frozen_base_policy(),
        repo_root=execution_repo,
        run_dir=campaign_dir,
        evaluate=benchmark.evaluate_ee,
        backend=benchmark._PinnedProotCodexBackend(),
        worktree_root=worktree_root,
        reference_metrics=baseline_metrics,
        memory_enabled=True,
        timeout_seconds=1_200,
    )
    result = M2EscapeCampaignController(runner=runner, policy=policy).run(
        generations=HORIZON
    )
    run_hash = hashlib.sha256(
        str(campaign_dir.resolve()).encode("utf-8")
    ).hexdigest()[:8]
    candidates: list[tuple[float, Path, str]] = [
        (float(baseline_metrics["raw_speedup"]), baseline, "SEED")
    ]
    successful = 0
    for generation in result.generations:
        for evaluation in generation.evaluations:
            candidate_id = evaluation.candidate_id
            key = f"{contract.campaign.id}-{run_hash}-{candidate_id}"
            source = runner.worktrees.candidate_path(key) / benchmark.CANDIDATE_RELATIVE
            if not source.is_file():
                continue
            receipt = json.loads(
                (campaign_dir / evaluation.receipt_path).read_text(encoding="utf-8")
            )["receipt"]["evaluation_input"]
            if bool(receipt["controls"].get("candidate_valid")):
                successful += 1
                candidates.append(
                    (float(receipt["metrics"]["raw_speedup"]), source, candidate_id)
                )
    _score, selected, selected_id = max(candidates, key=lambda item: item[0])
    selected_path = arm_dir / "selected_candidate.py"
    if selected.resolve() != selected_path.resolve():
        shutil.copy2(selected, selected_path)
    development = benchmark.evaluate_development(selected_path)
    arm_result = {
        "arm": arm,
        "candidate_path": str(selected_path.resolve()),
        "candidate_sha256": sha256_file(selected_path),
        "development": development,
        "tokens": benchmark._token_usage(arm_dir),
        "wall_seconds": time.perf_counter() - started,
        "proposal_valid_rate": successful / HORIZON,
        "model": benchmark.MODEL,
        "reasoning_effort": benchmark.REASONING_EFFORT,
        "metadata": {
            "engine": "EvidenceEvolve M2_ESCAPE_PROSPECTIVE",
            "policy_id": policy.policy_id,
            "selected_candidate_id": selected_id,
            "execution_commit": contract.campaign.base_commit,
            "scientific_memory_enabled": True,
            "implementer_sandbox_bridge": "proot-nobody",
            "search_mechanics_status": "PASS" if successful else "FAIL",
            "baseline_fallback": selected_id == "SEED",
            "evidence_scope": "DEVELOPMENT_ONLY",
            "blind_artifacts_read": False,
            "blind_evaluator_calls": 0,
        },
    }
    arm_dir.mkdir(parents=True, exist_ok=True)
    (arm_dir / "arm_result.json").write_text(
        json.dumps(arm_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return arm_result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one M2-R1B Set Cover development-only arm"
    )
    parser.add_argument("--arm", required=True, choices=tuple(ARMS))
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    for key, value in {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "EE_ALGOTUNE_DEV_COUNT": "100",
        "EE_ALGOTUNE_DEV_REPEATS": "3",
        "EE_ALGOTUNE_WORKERS": "3",
    }.items():
        os.environ[key] = value
    run_arm(args.run_dir.resolve(), args.arm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
