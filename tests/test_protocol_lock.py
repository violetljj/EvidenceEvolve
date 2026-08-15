from __future__ import annotations

import subprocess
from pathlib import Path

from evidence_evolve.governance.protocol_lock import (
    REQUIRED_HARNESS_CORE_PATHS,
    ProtocolLock,
)
from evidence_evolve.models import (
    Budgets,
    Campaign,
    EditableScope,
    EvidenceGrade,
    EvidencePermission,
    EvidenceSource,
    FrozenAsset,
    FrozenAssetKind,
    MetricConstraint,
    MetricsPolicy,
    ObjectiveDirection,
    ResearchContract,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_lock_hashes_assets_and_detects_drift(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    files = {
        "evaluators/evaluate.py": "print('fixed')\n",
        "adapters/evaluate.py": "def adapt(): return 'fixed'\n",
        "protocols/closures.yaml": 'schema_version: "1.0"\nclosures: []\n',
        "confirmation/manifest.json": "{}\n",
        "manifests/truth.json": "{}\n",
    }
    files.update({path: "# frozen harness core\n" for path in REQUIRED_HARNESS_CORE_PATHS})
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    draft = ResearchContract(
        campaign=Campaign(
            id="lock_test",
            base_commit="HEAD",
            research_question="Does contract locking detect frozen evaluator drift?",
            claim_scope="mechanics_only",
        ),
        editable_scope=EditableScope(
            allow=["candidates/**"],
            deny=[
                "adapters/**",
                "confirmation/**",
                "evaluators/**",
                "evidence_evolve/**",
                "protocols/**",
            ],
        ),
        evidence_sources=[
            EvidenceSource(
                source_id="truth",
                grade=EvidenceGrade.A,
                path="manifests/truth.json",
                permissions={EvidencePermission.DEV},
            )
        ],
        frozen_assets=[
            FrozenAsset(
                asset_id="evaluator",
                kind=FrozenAssetKind.EVALUATOR,
                path="evaluators/evaluate.py",
            ),
            FrozenAsset(
                asset_id="adapter",
                kind=FrozenAssetKind.ADAPTER,
                path="adapters/evaluate.py",
            ),
            *[
                FrozenAsset(
                    asset_id=f"harness-{index}",
                    kind=FrozenAssetKind.HARNESS_CORE,
                    path=path,
                )
                for index, path in enumerate(sorted(REQUIRED_HARNESS_CORE_PATHS))
            ],
            FrozenAsset(
                asset_id="closures",
                kind=FrozenAssetKind.PROTOCOL,
                path="protocols/closures.yaml",
            ),
            FrozenAsset(
                asset_id="confirmation",
                kind=FrozenAssetKind.CONFIRMATION,
                path="confirmation/manifest.json",
            ),
        ],
        metrics=MetricsPolicy(
            hard_constraints={"false_block": MetricConstraint(max=0)},
            pareto_objectives={"quality": ObjectiveDirection.MAXIMIZE},
        ),
        required_controls=["fixed_control"],
        budgets=Budgets(proposal_calls=1),
        closure_registry="protocols/closures.yaml",
    )
    locker = ProtocolLock(repo)
    locked = locker.lock(draft)
    assert locker.validate(locked).valid
    (repo / "evaluators/evaluate.py").write_text("print('tampered')\n", encoding="utf-8")
    drift = locker.validate(locked)
    assert not drift.valid
    assert "FROZEN_ASSET_HASH_MISMATCH:evaluator" in drift.issues
