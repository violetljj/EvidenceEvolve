from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

import yaml

from evidence_evolve.artifacts import atomic_write_json
from evidence_evolve.backends.codex_cli import CodexCliBackend
from evidence_evolve.discovery.autonomous import AutonomousCampaignRunner
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import ProtocolLock, dump_contract, load_contract
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_adapter import (
    CANDIDATE_PATH,
    evaluate_candidate,
    evaluate_program_twice,
)


TASK_DIR = Path("experiments/engine_bakeoff/minmax16")
CONTRACT_PATH = TASK_DIR / "evidence_contract.template.yaml"
POLICY_PATH = TASK_DIR / "evidence_policy.yaml"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


def _snapshot_paths(repo_root: Path) -> tuple[list[str], str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    paths = sorted(
        item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
        and not item.startswith((b"runs/", b".evolve-worktrees/"))
        and b"/__pycache__/" not in item
        and not item.endswith(b".pyc")
    )
    hashes = {path: sha256_file(repo_root / path) for path in paths}
    return paths, sha256_object(hashes)


def _prepare_execution_repo(source_root: Path, execution_repo: Path) -> None:
    paths, snapshot_sha256 = _snapshot_paths(source_root)
    manifest_path = execution_repo.parent / "execution_repo_snapshot.json"
    if execution_repo.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_snapshot_sha256"] != snapshot_sha256:
            raise ValueError("source snapshot changed after execution repo creation")
        return
    execution_repo.mkdir(parents=True)
    for relative in paths:
        source = source_root / relative
        destination = execution_repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    _git(execution_repo, "init", "-b", "master")
    _git(execution_repo, "add", ".")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=EvidenceEvolve Benchmark",
            "-c",
            "user.email=benchmark@invalid.local",
            "commit",
            "-m",
            "Frozen external benchmark execution snapshot",
        ],
        cwd=execution_repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        },
    )
    atomic_write_json(
        manifest_path,
        {
            "source_snapshot_sha256": snapshot_sha256,
            "execution_commit": _git(execution_repo, "rev-parse", "HEAD"),
            "file_count": len(paths),
        },
    )


def _token_usage(run_dir: Path) -> dict[str, int]:
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    for path in run_dir.rglob("*.events.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_usage = event.get("usage") if isinstance(event, dict) else None
            if not isinstance(event_usage, dict):
                continue
            for key in usage:
                usage[key] += int(event_usage.get(key, 0) or 0)
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _run_inside(repo_root: Path, run_dir: Path) -> None:
    contract = load_contract(repo_root / CONTRACT_PATH)
    contract.campaign.base_commit = _git(repo_root, "rev-parse", "HEAD")
    contract.lock = None
    contract = ProtocolLock(repo_root).lock(contract)
    ProtocolLock(repo_root).assert_valid(contract)
    dump_contract(contract, run_dir / "campaign_contract.locked.yaml")
    policy_payload = yaml.safe_load((repo_root / POLICY_PATH).read_text(encoding="utf-8"))
    policy = ResearchPolicyGenome.model_validate(policy_payload)
    wrapper = os.environ["EVIDENCE_EVOLVE_CODEX_EXECUTABLE"]
    backend = CodexCliBackend(wrapper)
    status = backend.status()
    if not status.get("usable"):
        raise RuntimeError(f"Codex backend unavailable: {status}")
    baseline = evaluate_program_twice(repo_root / CANDIDATE_PATH, repo_root)
    campaign_dir = run_dir / "campaign"
    worktree_digest = hashlib.sha256(str(run_dir).encode("utf-8")).hexdigest()[:12]
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(repo_root / contract.closure_registry),
        policy=policy,
        repo_root=repo_root,
        run_dir=campaign_dir,
        evaluate=evaluate_candidate,
        backend=backend,
        worktree_root=Path(tempfile.gettempdir()) / "ee-minmax16-wt" / worktree_digest,
        reference_metrics=baseline["metrics"],
        memory_enabled=True,
        timeout_seconds=900,
    )
    started = perf_counter()
    result = runner.run(
        generations=1,
        proposals_per_generation=1,
        max_evaluations_per_generation=1,
    )
    wall_seconds = perf_counter() - started
    candidate_id = "GEN-001-C01"
    run_hash = hashlib.sha256(str(campaign_dir.resolve()).encode("utf-8")).hexdigest()[:8]
    worktree_key = f"{contract.campaign.id}-{run_hash}-{candidate_id}"
    candidate_source = runner.worktrees.candidate_path(worktree_key) / CANDIDATE_PATH
    candidate_destination = run_dir / "submitted_candidates" / "C001.py"
    candidate_destination.parent.mkdir(parents=True, exist_ok=True)
    if candidate_source.is_file():
        shutil.copy2(candidate_source, candidate_destination)
        independent = evaluate_program_twice(candidate_destination, repo_root)
    else:
        independent = None
    evaluations = [
        evaluation
        for generation in result.generations
        for evaluation in generation.evaluations
    ]
    failures = [
        failure.model_dump(mode="json")
        for generation in result.generations
        for failure in generation.failures
    ]
    atomic_write_json(
        run_dir / "summary.json",
        {
            "engine": "EvidenceEvolve+Codex",
            "campaign_id": contract.campaign.id,
            "execution_commit": contract.campaign.base_commit,
            "wall_seconds": wall_seconds,
            "token_usage": _token_usage(campaign_dir),
            "baseline": baseline,
            "candidate_path": str(candidate_destination) if candidate_source.is_file() else None,
            "independent_replay": independent,
            "evaluations": [item.model_dump(mode="json") for item in evaluations],
            "failures": failures,
            "budgets": result.budgets,
            "claim_scope": contract.campaign.claim_scope,
            "blind_confirmation_available": False,
        },
    )
    print((run_dir / "summary.json").read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.inside:
        _run_inside(args.repo_root.resolve(), args.run_dir.resolve())
        return
    source_root = Path(__file__).resolve().parents[3]
    execution_repo = args.run_dir.resolve().parent / "evidence_execution_repo"
    _prepare_execution_repo(source_root, execution_repo)
    env = {
        **os.environ,
        "PYTHONPATH": str(execution_repo),
        "PATH": "/opt/node-v22.23.2-linux-x64/bin:" + os.environ.get("PATH", ""),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    subprocess.run(
        [
            sys.executable,
            str(execution_repo / TASK_DIR / "run_evidence_smoke.py"),
            "--inside",
            "--repo-root",
            str(execution_repo),
            "--run-dir",
            str(args.run_dir.resolve()),
        ],
        cwd=execution_repo,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
