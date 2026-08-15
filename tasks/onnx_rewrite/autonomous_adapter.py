from __future__ import annotations

import subprocess
from time import perf_counter

from evidence_evolve.discovery.autonomous import AutonomousEvaluationContext
from evidence_evolve.discovery.campaign import EvaluationRun
from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.onnx_campaign import build_onnx_evaluation
from evidence_evolve.worktrees import WorktreeManager
from tasks.onnx_rewrite.evaluator import evaluate


def evaluate_candidate(context: AutonomousEvaluationContext) -> EvaluationRun:
    """Frozen ONNX observation adapter; it never returns a verdict."""
    candidate_path = (
        context.worktree / "tasks" / "onnx_rewrite" / "candidates" / "candidate.py"
    )
    manager = WorktreeManager(context.repo_root)
    changed_files = manager.changed_files(
        context.worktree, context.contract.campaign.base_commit
    )
    started = perf_counter()
    raw = evaluate(candidate_path, confirmation=False)
    elapsed = perf_counter() - started
    evaluation = build_onnx_evaluation(
        contract_sha256=context.contract.lock.content_sha256,
        candidate=context.candidate.acquisition.candidate,
        stage=context.candidate.stage,
        changed_files=changed_files,
        protocol_violations=[],
        raw=raw,
    )
    patch = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            context.contract.campaign.base_commit,
            "--",
        ],
        cwd=context.worktree,
        check=True,
        capture_output=True,
    ).stdout
    candidate_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=context.worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return EvaluationRun(
        evaluation=evaluation,
        command=[
            "in-process",
            "tasks.onnx_rewrite.evaluator:evaluate",
            candidate_path.as_posix(),
        ],
        elapsed_seconds=elapsed,
        seed=int(raw.get("seed", 0)),
        candidate_commit=candidate_commit,
        patch_sha256=sha256_bytes(patch),
    )
