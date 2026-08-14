from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from pydantic import Field

from evidence_evolve.governance.evidence_policy import audit_source
from evidence_evolve.governance.scope import matches_any
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.models import (
    ContractLock,
    FrozenAssetKind,
    ResearchContract,
    StrictModel,
)


REQUIRED_HARNESS_CORE_PATHS = frozenset(
    {
        "evidence_evolve/archive.py",
        "evidence_evolve/artifacts.py",
        "evidence_evolve/budgets.py",
        "evidence_evolve/hashing.py",
        "evidence_evolve/models.py",
        "evidence_evolve/replay.py",
        "evidence_evolve/worktrees.py",
        "evidence_evolve/governance/candidate_auditor.py",
        "evidence_evolve/governance/closure_registry.py",
        "evidence_evolve/governance/evidence_policy.py",
        "evidence_evolve/governance/gate_engine.py",
        "evidence_evolve/governance/protocol_lock.py",
        "evidence_evolve/governance/scope.py",
    }
)


class ValidationReport(StrictModel):
    valid: bool
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    contract_sha256: str | None = None
    resolved_base_commit: str | None = None


class ContractValidationError(RuntimeError):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("; ".join(report.issues))


def load_contract(path: Path) -> ResearchContract:
    with path.open("r", encoding="utf-8") as stream:
        payload = yaml.safe_load(stream) or {}
    return ResearchContract.model_validate(payload)


def dump_contract(contract: ResearchContract, path: Path) -> None:
    payload = contract.model_dump(mode="json", exclude_none=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(payload, stream, sort_keys=False, allow_unicode=True)


def contract_content_hash(contract: ResearchContract) -> str:
    return sha256_object(contract.model_dump(mode="python", exclude={"lock"}))


class ProtocolLock:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _repo_path(self, relative: str) -> Path:
        target = (self.repo_root / relative).resolve()
        try:
            target.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError(f"path escapes repository: {relative}") from exc
        return target

    def _resolve_commit(self, revision: str) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def lock(self, draft: ResearchContract) -> ResearchContract:
        locked = draft.model_copy(deep=True)
        locked.campaign.base_commit = self._resolve_commit(draft.campaign.base_commit)
        for asset in locked.frozen_assets:
            path = self._repo_path(asset.path)
            if not path.is_file():
                raise FileNotFoundError(f"frozen asset is not a file: {asset.path}")
            asset.sha256 = sha256_file(path)
        for source in locked.evidence_sources:
            path = self._repo_path(source.path)
            if not path.is_file():
                raise FileNotFoundError(f"evidence manifest is not a file: {source.path}")
            source.sha256 = sha256_file(path)
        locked.lock = ContractLock(content_sha256=contract_content_hash(locked))
        return locked

    def validate(self, contract: ResearchContract) -> ValidationReport:
        issues: list[str] = []
        warnings: list[str] = []
        resolved_commit: str | None = None

        try:
            resolved_commit = self._resolve_commit(contract.campaign.base_commit)
        except (subprocess.CalledProcessError, FileNotFoundError):
            issues.append(f"BASE_COMMIT_NOT_FOUND:{contract.campaign.base_commit}")
        else:
            if contract.lock and contract.campaign.base_commit != resolved_commit:
                issues.append("LOCKED_BASE_COMMIT_MUST_BE_FULL_SHA")

        for source in contract.evidence_sources:
            issues.extend(audit_source(source))
            path = self._repo_path(source.path)
            if not path.is_file():
                issues.append(f"EVIDENCE_MANIFEST_MISSING:{source.source_id}:{source.path}")
            elif contract.lock:
                if source.sha256 is None:
                    issues.append(f"EVIDENCE_HASH_MISSING:{source.source_id}")
                elif sha256_file(path) != source.sha256.lower():
                    issues.append(f"EVIDENCE_HASH_MISMATCH:{source.source_id}")

        kinds = {asset.kind for asset in contract.frozen_assets}
        if FrozenAssetKind.EVALUATOR not in kinds:
            issues.append("NO_FROZEN_EVALUATOR")
        if FrozenAssetKind.ADAPTER not in kinds:
            issues.append("NO_FROZEN_EVALUATION_ADAPTER")
        if FrozenAssetKind.HARNESS_CORE not in kinds:
            issues.append("NO_FROZEN_HARNESS_CORE")
        if FrozenAssetKind.CONFIRMATION not in kinds:
            issues.append("NO_HIDDEN_CONFIRMATION_ASSET")

        registry_present = False
        for asset in contract.frozen_assets:
            path = self._repo_path(asset.path)
            if asset.path == contract.closure_registry and asset.kind == FrozenAssetKind.PROTOCOL:
                registry_present = True
            if not matches_any(asset.path, contract.editable_scope.deny):
                issues.append(f"FROZEN_ASSET_NOT_DENIED:{asset.asset_id}:{asset.path}")
            if not path.is_file():
                issues.append(f"FROZEN_ASSET_MISSING:{asset.asset_id}:{asset.path}")
            elif contract.lock:
                if asset.sha256 is None:
                    issues.append(f"FROZEN_ASSET_HASH_MISSING:{asset.asset_id}")
                elif sha256_file(path) != asset.sha256.lower():
                    issues.append(f"FROZEN_ASSET_HASH_MISMATCH:{asset.asset_id}")
        if not registry_present:
            issues.append("CLOSURE_REGISTRY_NOT_FROZEN_AS_PROTOCOL")

        frozen_core_paths = {
            asset.path
            for asset in contract.frozen_assets
            if asset.kind is FrozenAssetKind.HARNESS_CORE
        }
        for missing_path in sorted(REQUIRED_HARNESS_CORE_PATHS - frozen_core_paths):
            issues.append(f"REQUIRED_HARNESS_CORE_NOT_FROZEN:{missing_path}")

        if contract.lock is None:
            issues.append("CONTRACT_NOT_LOCKED")
        else:
            actual_hash = contract_content_hash(contract)
            if actual_hash != contract.lock.content_sha256:
                issues.append("CONTRACT_CONTENT_HASH_MISMATCH")

        for allow_pattern in contract.editable_scope.allow:
            if matches_any(allow_pattern, contract.editable_scope.deny):
                issues.append(f"ALLOW_PATTERN_DENIED:{allow_pattern}")

        if contract.budgets.confirmation_runs > 1:
            warnings.append("CONFIRMATION_BUDGET_GREATER_THAN_ONE")

        return ValidationReport(
            valid=not issues,
            issues=sorted(set(issues)),
            warnings=sorted(set(warnings)),
            contract_sha256=contract_content_hash(contract),
            resolved_base_commit=resolved_commit,
        )

    def assert_valid(self, contract: ResearchContract) -> ValidationReport:
        report = self.validate(contract)
        if not report.valid:
            raise ContractValidationError(report)
        return report
