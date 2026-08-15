from __future__ import annotations

import subprocess

from evidence_evolve.models import EditableScope
from evidence_evolve.worktrees import WorktreeManager


def _git(repo, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_candidate_worktrees_are_isolated_and_scope_audited(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    candidate_file = repo / "candidates" / "model.py"
    evaluator_file = repo / "evaluators" / "evaluate.py"
    candidate_file.parent.mkdir(parents=True)
    evaluator_file.parent.mkdir(parents=True)
    candidate_file.write_text("VALUE = 'seed'\n", encoding="utf-8")
    evaluator_file.write_text("VALUE = 'frozen'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    base = _git(repo, "rev-parse", "HEAD")
    manager = WorktreeManager(repo)
    first = manager.create("CANDIDATE-ONE", base)
    second = manager.create("CANDIDATE-TWO", base)

    (first / "candidates" / "model.py").write_text(
        "VALUE = 'candidate-one'\n", encoding="utf-8"
    )
    assert (second / "candidates" / "model.py").read_text(encoding="utf-8") == (
        "VALUE = 'seed'\n"
    )
    scope = EditableScope(
        allow=["candidates/**"],
        deny=["evaluators/**"],
    )
    assert manager.audit(first, base, scope) == []
    (first / "evaluators" / "evaluate.py").write_text(
        "VALUE = 'tampered'\n", encoding="utf-8"
    )
    assert manager.audit(first, base, scope) == [
        "DENIED_PATH:evaluators/evaluate.py"
    ]

    reference = manager.pin_commit("test-run", "CANDIDATE-ONE", base)
    assert reference == "refs/evidence-evolve/test-run/CANDIDATE-ONE"
    assert _git(repo, "rev-parse", reference) == base
    assert manager.pin_commit("test-run", "CANDIDATE-ONE", base) == reference
