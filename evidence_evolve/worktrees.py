from __future__ import annotations

import re
import subprocess
from pathlib import Path

from evidence_evolve.governance.scope import audit_paths
from evidence_evolve.models import EditableScope


class WorktreeManager:
    def __init__(self, repo_root: Path, worktree_root: Path | None = None):
        self.repo_root = repo_root.resolve()
        self.worktree_root = (
            worktree_root.resolve()
            if worktree_root
            else (self.repo_root / ".evolve-worktrees").resolve()
        )

    @staticmethod
    def _safe_candidate_name(candidate_id: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate_id).strip(".-")
        if not name:
            raise ValueError("candidate id has no safe filesystem representation")
        return name

    def candidate_path(self, candidate_id: str) -> Path:
        target = (self.worktree_root / self._safe_candidate_name(candidate_id)).resolve()
        target.relative_to(self.worktree_root)
        return target

    def create(self, candidate_id: str, base_commit: str) -> Path:
        target = self.candidate_path(candidate_id)
        if target.exists():
            raise FileExistsError(f"candidate worktree already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(target), base_commit],
            cwd=self.repo_root,
            check=True,
        )
        return target

    def changed_files(self, worktree: Path, base_commit: str) -> list[str]:
        completed = subprocess.run(
            ["git", "diff", "--name-only", base_commit, "--"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
        tracked = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        untracked_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
        untracked = [
            line.strip() for line in untracked_result.stdout.splitlines() if line.strip()
        ]
        return sorted(set(tracked + untracked))

    def audit(self, worktree: Path, base_commit: str, scope: EditableScope) -> list[str]:
        return audit_paths(scope, self.changed_files(worktree, base_commit))

