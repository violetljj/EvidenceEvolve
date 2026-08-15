from __future__ import annotations

import subprocess
from time import perf_counter

from evidence_evolve.discovery.autonomous import AutonomousEvaluationContext
from evidence_evolve.discovery.campaign import EvaluationRun
from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.worktrees import WorktreeManager
from tasks.algotune_set_cover.campaign_evaluator import (
    build_evaluation,
    evaluate_development,
)


def evaluate_candidate(context: AutonomousEvaluationContext) -> EvaluationRun:
    candidate_path = context.worktree / "tasks" / "algotune_set_cover" / "initial.py"
    changed_files = WorktreeManager(context.repo_root).changed_files(
        context.worktree, context.genetic_parent_commit
    )
    started = perf_counter()
    raw = evaluate_development(candidate_path)
    elapsed = perf_counter() - started
    evaluation = build_evaluation(
        contract_sha256=context.contract.lock.content_sha256,
        candidate=context.candidate,
        changed_files=changed_files,
        raw=raw,
    )
    patch = subprocess.run(
        ["git", "diff", "--binary", context.genetic_parent_commit, "--"],
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
        command=["in-process", "algotune-set-cover-development-evaluator"],
        elapsed_seconds=elapsed,
        seed=0,
        candidate_commit=candidate_commit,
        patch_sha256=sha256_bytes(patch),
    )
