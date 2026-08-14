from __future__ import annotations

from fnmatch import fnmatchcase

from evidence_evolve.models import EditableScope


def normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def matches_any(path: str, patterns: list[str]) -> bool:
    normalized = normalize_repo_path(path)
    return any(fnmatchcase(normalized, pattern) for pattern in patterns)


def audit_paths(scope: EditableScope, paths: list[str]) -> list[str]:
    violations: list[str] = []
    for raw_path in paths:
        path = normalize_repo_path(raw_path)
        if matches_any(path, scope.deny):
            violations.append(f"DENIED_PATH:{path}")
        elif not matches_any(path, scope.allow):
            violations.append(f"OUTSIDE_ALLOW_SCOPE:{path}")
    return sorted(set(violations))

