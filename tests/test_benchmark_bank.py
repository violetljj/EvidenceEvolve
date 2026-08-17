from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_evolve.benchmark_bank import (
    CORE_12_TASK_IDS,
    BankAsset,
    BenchmarkBankValidator,
    ClaimCeiling,
    DifficultyLevel,
    EvidenceRole,
    load_bank_manifest,
    select_families,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "benchmark_bank" / "manifest.v1.yaml"


def test_core12_catalog_lock_and_local_assets_validate() -> None:
    manifest = load_bank_manifest(MANIFEST)
    report = BenchmarkBankValidator(REPO_ROOT).validate(manifest)

    assert report.valid
    assert tuple(manifest.core_family_ids) == CORE_12_TASK_IDS
    assert report.materialized_asset_count == 3
    assert report.catalog_only_asset_count == 15
    assert report.executable_family_count == 0
    assert "PUBLIC_BENCHMARKS_ARE_NOT_BLIND" in report.warnings
    assert "NO_CORE_FAMILY_HAS_FROZEN_INSTANCE_INVENTORY" in report.warnings
    assert manifest.locked_registry == []


def test_evidence_tiers_and_public_roles_are_claim_bounded() -> None:
    manifest = load_bank_manifest(MANIFEST)

    for family in manifest.families:
        assert EvidenceRole.LOCKED not in family.allowed_roles
        if family.difficulty in {DifficultyLevel.L0, DifficultyLevel.L1}:
            assert family.claim_ceiling is ClaimCeiling.SCREENING_ONLY
        if family.difficulty in {
            DifficultyLevel.L3,
            DifficultyLevel.L4,
            DifficultyLevel.L5,
        }:
            assert (
                family.claim_ceiling
                is ClaimCeiling.RESEARCH_VALUE_EVALUATION_ELIGIBLE
            )


def test_family_selection_is_deterministic_and_uses_the_bank() -> None:
    manifest = load_bank_manifest(MANIFEST)

    first = select_families(manifest, "routine_dev_3x3", seed=170817)
    second = select_families(manifest, "routine_dev_3x3", seed=170817)
    signal = select_families(manifest, "signal_validation_8x5", seed=170817)
    milestone = select_families(manifest, "milestone_core12", seed=170817)

    assert first == second
    assert set(first.family_ids) == {"assignment", "set_cover", "graph_coloring"}
    assert first.instances_per_family == 3
    assert len(signal.family_ids) == 8
    assert "assignment" not in signal.family_ids
    assert "knapsack" not in signal.family_ids
    assert milestone.family_ids == list(CORE_12_TASK_IDS)
    assert first.planning_only is True
    assert first.authority == "NO_SCIENTIFIC_CLAIM"


def test_validator_fails_closed_on_local_asset_or_catalog_tampering() -> None:
    manifest = load_bank_manifest(MANIFEST)
    manifest.families[0].assets[0].sha256 = "0" * 64
    report = BenchmarkBankValidator(REPO_ROOT).validate(manifest)

    assert not report.valid
    assert any("BANK_ASSET_HASH_MISMATCH:assignment" in item for item in report.issues)
    assert "BENCHMARK_BANK_CONTENT_HASH_MISMATCH" in report.issues


def test_catalog_asset_cannot_claim_a_hash_or_escape_the_repository() -> None:
    with pytest.raises(ValidationError):
        BankAsset.model_validate(
            {
                "asset_id": "invalid_asset",
                "kind": "INSTANCE_SOURCE",
                "state": "CATALOG_ONLY",
                "authority": "test",
                "source_url": "https://example.test/archive",
                "local_path": "../escape",
                "sha256": "0" * 64,
                "license_status": "REVIEW_REQUIRED_BEFORE_DOWNLOAD",
                "public_visibility": True,
                "notes": "invalid on purpose",
            }
        )
