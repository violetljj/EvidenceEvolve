from __future__ import annotations

from pathlib import Path

import pytest

from evidence_evolve.hashing import sha256_object
from evidence_evolve.models import (
    Authority,
    Budgets,
    Campaign,
    CandidateGenome,
    ContractLock,
    EditableScope,
    EvidenceGrade,
    EvidencePermission,
    EvidenceSource,
    ExpectedSignature,
    FrozenAsset,
    FrozenAssetKind,
    MetricConstraint,
    MetricsPolicy,
    MutationType,
    ObjectiveDirection,
    ResearchContract,
)


@pytest.fixture
def candidate() -> CandidateGenome:
    return CandidateGenome(
        candidate_id="TEST-CANDIDATE-001",
        parent_ids=["SEED"],
        island="representation",
        family="new_representation",
        mutation_type=MutationType.REPRESENTATION,
        hypothesis="A separated representation improves long-horizon clearance behavior.",
        intervention="Add one isolated representation block.",
        expected_signature=ExpectedSignature(
            improve=["clearance_mae_delta"],
            unchanged=["false_block_delta_pp"],
        ),
        falsifier="Wrong-factor controls perform as well as the proposed factor.",
        required_controls=["wrong_factor", "zero_factor"],
        editable_files=["candidates/model.py"],
        estimated_cost_tier=2,
    )


@pytest.fixture
def contract() -> ResearchContract:
    value = ResearchContract(
        campaign=Campaign(
            id="test_campaign",
            base_commit="a" * 40,
            research_question="Does the bounded candidate pass deterministic scientific gates?",
            claim_scope="mechanics_only",
        ),
        authority=Authority(),
        editable_scope=EditableScope(
            allow=["candidates/**"],
            deny=["evaluators/**", "protocols/**", "confirmation/**"],
        ),
        evidence_sources=[
            EvidenceSource(
                source_id="truth-a",
                grade=EvidenceGrade.A,
                path="manifests/truth.json",
                permissions={
                    EvidencePermission.TRAIN,
                    EvidencePermission.DEV,
                    EvidencePermission.CONFIRM,
                    EvidencePermission.CLAIM,
                },
                sha256="b" * 64,
            )
        ],
        frozen_assets=[
            FrozenAsset(
                asset_id="evaluator",
                kind=FrozenAssetKind.EVALUATOR,
                path="evaluators/evaluate.py",
                sha256="c" * 64,
            ),
            FrozenAsset(
                asset_id="harness",
                kind=FrozenAssetKind.HARNESS_CORE,
                path="protocols/gate.py",
                sha256="f" * 64,
            ),
            FrozenAsset(
                asset_id="closures",
                kind=FrozenAssetKind.PROTOCOL,
                path="protocols/closures.yaml",
                sha256="d" * 64,
            ),
            FrozenAsset(
                asset_id="confirmation",
                kind=FrozenAssetKind.CONFIRMATION,
                path="confirmation/manifest.json",
                sha256="e" * 64,
            ),
        ],
        metrics=MetricsPolicy(
            hard_constraints={
                "false_block_delta_pp": MetricConstraint(max=0.0),
            },
            pareto_objectives={
                "clearance_mae_delta": ObjectiveDirection.MINIMIZE,
                "false_block_delta_pp": ObjectiveDirection.MINIMIZE,
            },
        ),
        required_controls=["wrong_factor", "zero_factor"],
        budgets=Budgets(proposal_calls=2, mechanics_runs=2),
        closure_registry="protocols/closures.yaml",
    )
    value.lock = ContractLock(
        content_sha256=sha256_object(value.model_dump(mode="python", exclude={"lock"}))
    )
    return value
