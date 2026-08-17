from __future__ import annotations

import argparse
import json
import random
import sys
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.benchmark_bank_smoke import (
    load_smoke_inventory,
    validate_smoke_inventory,
)
from evidence_evolve.models import StrictModel


CORE_12_TASK_IDS = (
    "assignment",
    "knapsack",
    "set_cover",
    "graph_coloring",
    "steiner_tree",
    "cvrp",
    "flexible_job_shop",
    "cluster_editing",
    "directed_feedback_vertex_set",
    "twinwidth",
    "dominating_set",
    "maximum_agreement_forest",
)


class DifficultyLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"


class EvidenceRole(StrEnum):
    DEV_REUSE = "DEV_REUSE"
    REGRESSION = "REGRESSION"
    BENCHMARK = "BENCHMARK"
    LOCKED = "LOCKED"


class ClaimCeiling(StrEnum):
    SCREENING_ONLY = "SCREENING_ONLY"
    DEVELOPMENT_COMPARISON_ONLY = "DEVELOPMENT_COMPARISON_ONLY"
    RESEARCH_VALUE_EVALUATION_ELIGIBLE = "RESEARCH_VALUE_EVALUATION_ELIGIBLE"
    GENERALIZATION_VERDICT_ELIGIBLE = "GENERALIZATION_VERDICT_ELIGIBLE"


class AssetState(StrEnum):
    CATALOG_ONLY = "CATALOG_ONLY"
    MATERIALIZED_LOCAL = "MATERIALIZED_LOCAL"
    MATERIALIZED_BY_RECEIPT = "MATERIALIZED_BY_RECEIPT"


class LicenseStatus(StrEnum):
    REPOSITORY_APACHE_2_0 = "REPOSITORY_APACHE_2_0"
    OFFICIAL_OPEN_SOURCE = "OFFICIAL_OPEN_SOURCE"
    REVIEW_REQUIRED_BEFORE_DOWNLOAD = "REVIEW_REQUIRED_BEFORE_DOWNLOAD"
    LOCAL_CACHE_NO_REDISTRIBUTION_CLAIM = "LOCAL_CACHE_NO_REDISTRIBUTION_CLAIM"


class BankAsset(StrictModel):
    asset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,127}$")
    kind: Literal["INSTANCE_SOURCE", "VERIFIER", "SCORER", "LOCAL_ADAPTER"]
    state: AssetState
    authority: str = Field(min_length=1)
    source_url: str | None = None
    source_revision: str | None = None
    local_path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    receipt_asset_id: str | None = None
    license_status: LicenseStatus
    public_visibility: Literal[True] = True
    notes: str = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def require_https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("benchmark source URLs must use HTTPS")
        return value

    @field_validator("local_path")
    @classmethod
    def repository_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts or normalized.startswith("/"):
            raise ValueError("benchmark bank asset path must stay inside the repository")
        return normalized

    @model_validator(mode="after")
    def state_matches_binding(self) -> "BankAsset":
        if self.state is AssetState.MATERIALIZED_LOCAL:
            if self.local_path is None or self.sha256 is None:
                raise ValueError("materialized assets require local_path and sha256")
            if self.receipt_asset_id is not None:
                raise ValueError("tracked materialized assets cannot use a receipt")
        elif self.state is AssetState.MATERIALIZED_BY_RECEIPT:
            if self.receipt_asset_id is None:
                raise ValueError("receipt-bound assets require receipt_asset_id")
            if self.local_path is not None or self.sha256 is not None:
                raise ValueError("receipt-bound paths and hashes belong in the receipt")
        elif (
            self.local_path is not None
            or self.sha256 is not None
            or self.receipt_asset_id is not None
        ):
            raise ValueError("catalog-only assets cannot claim a local path or hash")
        if self.source_url is None and self.local_path is None:
            raise ValueError("asset requires a source_url or a local_path")
        return self


class MaterializedAssetReceipt(StrictModel):
    asset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,127}$")
    local_path: str
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str
    source_revision: str

    @field_validator("local_path")
    @classmethod
    def repository_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts or normalized.startswith("/"):
            raise ValueError("materialized asset receipt path must stay in repository")
        return normalized

    @field_validator("source_url")
    @classmethod
    def require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("materialized source URLs must use HTTPS")
        return value


class MaterializationReceipt(StrictModel):
    schema_version: Literal["1.0"]
    generated_on: str = Field(pattern=r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$")
    assets: list[MaterializedAssetReceipt] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_assets(self) -> "MaterializationReceipt":
        ids = [asset.asset_id for asset in self.assets]
        paths = [asset.local_path for asset in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("materialization receipt asset ids must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("materialization receipt paths must be unique")
        return self


class BenchmarkFamily(StrictModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=1)
    difficulty: DifficultyLevel
    problem_domain: str = Field(min_length=1)
    optimization_modes: list[str] = Field(min_length=1)
    algorithmic_routes: list[str] = Field(min_length=1)
    default_role: EvidenceRole
    allowed_roles: list[EvidenceRole] = Field(min_length=1)
    claim_ceiling: ClaimCeiling
    assets: list[BankAsset] = Field(min_length=1)
    instance_manifest_paths: list[str] = Field(default_factory=list)
    known_failure_modes: list[str] = Field(default_factory=list)
    notes: str = Field(min_length=1)

    @field_validator("instance_manifest_paths")
    @classmethod
    def relative_instance_manifests(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.replace("\\", "/")
            path = Path(item)
            if path.is_absolute() or ".." in path.parts or item.startswith("/"):
                raise ValueError("instance manifest path must stay inside the repository")
            normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def evidence_invariants(self) -> "BenchmarkFamily":
        if len(self.allowed_roles) != len(set(self.allowed_roles)):
            raise ValueError("allowed_roles must be unique")
        if self.default_role not in self.allowed_roles:
            raise ValueError("default_role must be included in allowed_roles")
        if self.default_role is EvidenceRole.LOCKED:
            raise ValueError("public catalog families cannot default to LOCKED")
        if EvidenceRole.LOCKED in self.allowed_roles:
            raise ValueError("public catalog assets cannot be relabeled LOCKED")
        if self.difficulty in {DifficultyLevel.L0, DifficultyLevel.L1}:
            if self.claim_ceiling is not ClaimCeiling.SCREENING_ONLY:
                raise ValueError("L0/L1 families are screening-only")
        if self.claim_ceiling is ClaimCeiling.GENERALIZATION_VERDICT_ELIGIBLE:
            raise ValueError("public benchmark families cannot authorize generalization")
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset ids must be unique within a family")
        return self


class SelectionTemplate(StrictModel):
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    purpose: Literal["ROUTINE_DEVELOPMENT", "SIGNAL_VALIDATION", "MILESTONE"]
    family_count: int | None = Field(default=None, ge=1)
    instances_per_family: int = Field(ge=1)
    allowed_roles: list[EvidenceRole] = Field(min_length=1)
    minimum_difficulty: DifficultyLevel = DifficultyLevel.L0
    require_all_core_families: bool = False
    claim_ceiling: ClaimCeiling

    @model_validator(mode="after")
    def coherent_selection(self) -> "SelectionTemplate":
        if self.require_all_core_families and self.family_count is not None:
            raise ValueError("all-core templates cannot also set family_count")
        if not self.require_all_core_families and self.family_count is None:
            raise ValueError("non-all-core templates require family_count")
        if EvidenceRole.LOCKED in self.allowed_roles:
            raise ValueError("bank selection templates cannot consume LOCKED evidence")
        return self


class PortfolioPolicy(StrictModel):
    reused_percent: Literal[70] = 70
    rotating_percent: Literal[20] = 20
    fresh_blind_percent: Literal[10] = 10
    mandatory_per_round: Literal[False] = False
    fresh_may_be_zero_during_development: Literal[True] = True


class FreshGate(StrictModel):
    default_action: Literal["REUSE_BANK"] = "REUSE_BANK"
    require_predeclared_signal_gate: Literal[True] = True
    require_candidate_hash_lock: Literal[True] = True
    require_budget_and_evaluator_lock: Literal[True] = True
    require_new_unexposed_instances: Literal[True] = True
    downgrade_after_feedback: Literal["CONSUMED_VALIDATION_OR_DEV"] = (
        "CONSUMED_VALIDATION_OR_DEV"
    )


class BankLock(StrictModel):
    algorithm: Literal["sha256"] = "sha256"
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BenchmarkBankManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    bank_id: Literal["EVIDENCE_EVOLVE_BENCHMARK_BANK_V1"] = (
        "EVIDENCE_EVOLVE_BENCHMARK_BANK_V1"
    )
    status: Literal["CORE12_MATERIALIZED_SMOKE_ADMITTED"]
    verified_on: str = Field(pattern=r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$")
    core_family_ids: list[str]
    families: list[BenchmarkFamily]
    selection_templates: list[SelectionTemplate]
    portfolio_policy: PortfolioPolicy
    fresh_gate: FreshGate
    locked_registry: list[str] = Field(default_factory=list)
    materialization_receipt_path: str
    materialization_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lock: BankLock

    @model_validator(mode="after")
    def bank_invariants(self) -> "BenchmarkBankManifest":
        family_ids = [family.task_id for family in self.families]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("benchmark family ids must be unique")
        if tuple(self.core_family_ids) != CORE_12_TASK_IDS:
            raise ValueError("Core-12 ids or order changed")
        if set(family_ids) != set(CORE_12_TASK_IDS):
            raise ValueError("manifest must contain exactly the Core-12 families")
        template_ids = [item.template_id for item in self.selection_templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("selection template ids must be unique")
        if self.locked_registry:
            raise ValueError("v1 contains no blind assets; locked_registry must be empty")
        return self


class BankValidation(StrictModel):
    valid: bool
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    content_sha256: str
    materialized_asset_count: int = Field(ge=0)
    catalog_only_asset_count: int = Field(ge=0)
    executable_family_count: int = Field(ge=0)
    smoke_admitted_family_count: int = Field(ge=0)
    locally_available_receipt_asset_count: int = Field(ge=0)


class FamilySelection(StrictModel):
    template_id: str
    seed: int
    family_ids: list[str]
    instances_per_family: int
    planning_only: Literal[True] = True
    authority: Literal["NO_SCIENTIFIC_CLAIM"] = "NO_SCIENTIFIC_CLAIM"


def load_bank_manifest(path: Path) -> BenchmarkBankManifest:
    with path.open("r", encoding="utf-8") as stream:
        if path.suffix.lower() == ".json":
            payload = json.load(stream)
        else:
            payload = yaml.safe_load(stream)
    return BenchmarkBankManifest.model_validate(payload or {})


def load_materialization_receipt(path: Path) -> MaterializationReceipt:
    with path.open("r", encoding="utf-8") as stream:
        payload = yaml.safe_load(stream)
    return MaterializationReceipt.model_validate(payload or {})


def bank_content_hash(manifest: BenchmarkBankManifest) -> str:
    return sha256_object(manifest.model_dump(mode="python", exclude={"lock"}))


class BenchmarkBankValidator:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _repo_path(self, relative: str) -> Path:
        target = (self.repo_root / relative).resolve()
        try:
            target.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError(f"benchmark bank path escapes repository: {relative}") from exc
        return target

    def validate(self, manifest: BenchmarkBankManifest) -> BankValidation:
        issues: list[str] = []
        warnings: list[str] = [
            "PUBLIC_BENCHMARKS_ARE_NOT_BLIND",
            "SMOKE_ADMISSION_IS_NOT_FULL_RUNNER_ADMISSION",
        ]
        materialized = 0
        catalog_only = 0
        executable_families = 0
        smoke_admitted: set[str] = set()
        loaded_manifests: dict[str, set[str]] = {}
        locally_available_receipts = 0
        receipt_assets: dict[str, MaterializedAssetReceipt] = {}
        try:
            receipt_path = self._repo_path(manifest.materialization_receipt_path)
            if sha256_file(receipt_path) != manifest.materialization_receipt_sha256:
                raise ValueError("materialization receipt hash mismatch")
            receipt = load_materialization_receipt(receipt_path)
            receipt_assets = {asset.asset_id: asset for asset in receipt.assets}
            for asset in receipt.assets:
                path = self._repo_path(asset.local_path)
                if not path.is_file():
                    warnings.append(f"RECEIPT_ASSET_NOT_LOCAL:{asset.asset_id}")
                elif path.stat().st_size != asset.bytes or sha256_file(path) != asset.sha256:
                    issues.append(f"RECEIPT_ASSET_HASH_MISMATCH:{asset.asset_id}")
                else:
                    locally_available_receipts += 1
        except (FileNotFoundError, ValueError) as exc:
            issues.append(f"MATERIALIZATION_RECEIPT_INVALID:{type(exc).__name__}")
        for family in manifest.families:
            local_assets_valid = True
            for asset in family.assets:
                if asset.state is AssetState.CATALOG_ONLY:
                    catalog_only += 1
                    continue
                materialized += 1
                if asset.state is AssetState.MATERIALIZED_BY_RECEIPT:
                    assert asset.receipt_asset_id is not None
                    if asset.receipt_asset_id not in receipt_assets:
                        issues.append(
                            f"RECEIPT_BINDING_MISSING:{family.task_id}:{asset.asset_id}"
                        )
                        local_assets_valid = False
                    continue
                assert asset.local_path is not None
                assert asset.sha256 is not None
                path = self._repo_path(asset.local_path)
                if not path.is_file():
                    issues.append(f"BANK_ASSET_MISSING:{family.task_id}:{asset.asset_id}")
                    local_assets_valid = False
                elif sha256_file(path) != asset.sha256:
                    issues.append(
                        f"BANK_ASSET_HASH_MISMATCH:{family.task_id}:{asset.asset_id}"
                    )
                    local_assets_valid = False
            manifests_valid = bool(family.instance_manifest_paths)
            for relative in family.instance_manifest_paths:
                manifest_path = self._repo_path(relative)
                if not manifest_path.is_file():
                    issues.append(f"INSTANCE_MANIFEST_MISSING:{family.task_id}:{relative}")
                    manifests_valid = False
                    continue
                if relative not in loaded_manifests:
                    try:
                        inventory = load_smoke_inventory(manifest_path)
                        results = validate_smoke_inventory(inventory, CORE_12_TASK_IDS)
                        loaded_manifests[relative] = set(results)
                        smoke_admitted.update(results)
                    except (AssertionError, KeyError, TypeError, ValueError) as exc:
                        issues.append(
                            f"SMOKE_INVENTORY_INVALID:{relative}:{type(exc).__name__}"
                        )
                        loaded_manifests[relative] = set()
                if family.task_id not in loaded_manifests[relative]:
                    issues.append(
                        f"FAMILY_SMOKE_CASE_MISSING:{family.task_id}:{relative}"
                    )
                    manifests_valid = False
            if local_assets_valid and manifests_valid:
                executable_families += 1
        content_hash = bank_content_hash(manifest)
        if manifest.lock.content_sha256 != content_hash:
            issues.append("BENCHMARK_BANK_CONTENT_HASH_MISMATCH")
        return BankValidation(
            valid=not issues,
            issues=sorted(set(issues)),
            warnings=sorted(set(warnings)),
            content_sha256=content_hash,
            materialized_asset_count=materialized,
            catalog_only_asset_count=catalog_only,
            executable_family_count=executable_families,
            smoke_admitted_family_count=len(smoke_admitted),
            locally_available_receipt_asset_count=locally_available_receipts,
        )

    def assert_valid(self, manifest: BenchmarkBankManifest) -> BankValidation:
        report = self.validate(manifest)
        if not report.valid:
            raise ValueError("; ".join(report.issues))
        return report


def select_families(
    manifest: BenchmarkBankManifest,
    template_id: str,
    *,
    seed: int,
) -> FamilySelection:
    template = next(
        (item for item in manifest.selection_templates if item.template_id == template_id),
        None,
    )
    if template is None:
        raise ValueError(f"unknown benchmark bank template: {template_id}")
    minimum = int(template.minimum_difficulty.value[1:])
    eligible = [
        family.task_id
        for family in manifest.families
        if family.default_role in template.allowed_roles
        and int(family.difficulty.value[1:]) >= minimum
    ]
    if template.require_all_core_families:
        selected = list(manifest.core_family_ids)
    else:
        assert template.family_count is not None
        if len(eligible) < template.family_count:
            raise ValueError(
                f"template {template_id} requires {template.family_count} families; "
                f"only {len(eligible)} are eligible"
            )
        selected = random.Random(seed).sample(eligible, template.family_count)
    return FamilySelection(
        template_id=template_id,
        seed=seed,
        family_ids=selected,
        instances_per_family=template.instances_per_family,
    )


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EvidenceEvolve Benchmark Bank v1")
    parser.add_argument("--repo", default=".")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("manifest")
    select = subparsers.add_parser("select")
    select.add_argument("manifest")
    select.add_argument("--template", required=True)
    select.add_argument("--seed", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_bank_manifest(Path(args.manifest))
        report = BenchmarkBankValidator(Path(args.repo)).assert_valid(manifest)
        if args.command == "validate":
            print(_json(report))
        else:
            print(_json(select_families(manifest, args.template, seed=args.seed)))
        return 0
    except (FileNotFoundError, ValueError) as exc:
        print(_json({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
