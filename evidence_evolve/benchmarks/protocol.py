from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field

from evidence_evolve.benchmarks.models import (
    BenchmarkProtocol,
    BenchmarkProtocolLockData,
)
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.models import StrictModel


class BenchmarkProtocolValidation(StrictModel):
    valid: bool
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    content_sha256: str


def load_benchmark_protocol(path: Path) -> BenchmarkProtocol:
    with path.open("r", encoding="utf-8") as stream:
        return BenchmarkProtocol.model_validate(yaml.safe_load(stream) or {})


def dump_benchmark_protocol(protocol: BenchmarkProtocol, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = protocol.model_dump(mode="json", exclude_none=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(payload, stream, sort_keys=False, allow_unicode=True)


def benchmark_protocol_content_hash(protocol: BenchmarkProtocol) -> str:
    return sha256_object(protocol.model_dump(mode="python", exclude={"lock"}))


class BenchmarkProtocolLock:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _repo_path(self, relative: str) -> Path:
        target = (self.repo_root / relative).resolve()
        try:
            target.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError(f"benchmark asset escapes repository: {relative}") from exc
        return target

    def lock(self, draft: BenchmarkProtocol) -> BenchmarkProtocol:
        locked = draft.model_copy(deep=True)
        for asset in locked.assets:
            path = self._repo_path(asset.path)
            if not path.is_file():
                raise FileNotFoundError(f"benchmark asset is not a file: {asset.path}")
            asset.sha256 = sha256_file(path)
        locked.lock = BenchmarkProtocolLockData(
            content_sha256=benchmark_protocol_content_hash(locked)
        )
        return locked

    def validate(self, protocol: BenchmarkProtocol) -> BenchmarkProtocolValidation:
        issues: list[str] = []
        warnings = [
            "PUBLIC_FRESH_IS_NOT_BLIND_CONFIRMATION",
            "SUPERIORITY_CLAIMS_REQUIRE_EXTERNAL_BLIND_CONFIRMATION",
        ]
        for asset in protocol.assets:
            path = self._repo_path(asset.path)
            if not path.is_file():
                issues.append(f"BENCHMARK_ASSET_MISSING:{asset.asset_id}")
            elif protocol.lock is not None:
                if asset.sha256 is None:
                    issues.append(f"BENCHMARK_ASSET_HASH_MISSING:{asset.asset_id}")
                elif sha256_file(path) != asset.sha256:
                    issues.append(f"BENCHMARK_ASSET_HASH_MISMATCH:{asset.asset_id}")
        content_sha256 = benchmark_protocol_content_hash(protocol)
        if protocol.lock is None:
            issues.append("BENCHMARK_PROTOCOL_NOT_LOCKED")
        elif protocol.lock.content_sha256 != content_sha256:
            issues.append("BENCHMARK_PROTOCOL_CONTENT_HASH_MISMATCH")
        return BenchmarkProtocolValidation(
            valid=not issues,
            issues=sorted(set(issues)),
            warnings=warnings,
            content_sha256=content_sha256,
        )

    def assert_valid(self, protocol: BenchmarkProtocol) -> BenchmarkProtocolValidation:
        report = self.validate(protocol)
        if not report.valid:
            raise ValueError("; ".join(report.issues))
        return report
