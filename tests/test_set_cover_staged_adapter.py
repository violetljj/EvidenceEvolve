from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evidence_evolve.discovery.async_autonomous import MaterializedCandidate
from evidence_evolve.discovery.campaign import CampaignCandidate
from evidence_evolve.discovery.throughput import CandidateTicket, FunnelStage, StageStatus
from evidence_evolve.hashing import sha256_object
from evidence_evolve.meta_evolution.policy import AcquisitionSignals, CandidateAcquisition
from evidence_evolve.models import (
    CandidateGenome,
    ContractLock,
    ExpectedSignature,
    MetricConstraint,
    MetricsPolicy,
    MutationType,
    ObjectiveDirection,
    ScientificOutcome,
)
from tasks.algotune_set_cover.staged_adapter import (
    SetCoverFunnelPolicy,
    SetCoverStagedAdapter,
    SetCoverStructuralTransitionAudit,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _materialized(tmp_path: Path, contract, value: str) -> MaterializedCandidate:
    worktree = tmp_path / value
    source = worktree / "tasks/algotune_set_cover/initial.py"
    source.parent.mkdir(parents=True)
    source.write_text(value, encoding="utf-8")
    genome = CandidateGenome(
        candidate_id=f"CAND-{value}",
        parent_ids=["SEED"],
        island="main",
        family=f"family-{value}",
        mutation_type=MutationType.MECHANISM,
        hypothesis="A staged Set Cover candidate changes the core solving mechanism.",
        intervention="Replace the bounded solver implementation for this candidate.",
        expected_signature=ExpectedSignature(improve=["raw_speedup"]),
        falsifier="The candidate is invalid or fails to improve full development.",
        required_controls=["candidate_valid", "development_only"],
        editable_files=["tasks/algotune_set_cover/initial.py"],
        estimated_cost_tier=1,
    )
    item = CampaignCandidate(
        acquisition=CandidateAcquisition(
            candidate=genome,
            signals=AcquisitionSignals(
                admit_probability=0.8,
                expected_improvement=0.2,
                information_gain=0.7,
                novelty=0.7,
            ),
        )
    )
    return MaterializedCandidate(
        generation_id="WAVE-001",
        item=item,
        worktree=worktree,
        changed_files=["tasks/algotune_set_cover/initial.py"],
        genetic_parent_id="SEED",
        genetic_parent_commit="a" * 40,
        candidate_commit="b" * 40,
        candidate_ref="refs/test",
        patch_sha256="c" * 64,
        parent_patch_sha256="d" * 64,
    )


def _ticket(candidate_id: str) -> CandidateTicket:
    return CandidateTicket(
        candidate_id=candidate_id,
        dispatch_index=1,
        lineage_id="LINEAGE",
        operator_class="structural",
        genetic_parent_id="SEED",
        requires_structural_transition=True,
    )


def test_set_cover_staged_adapter_preserves_stage_authority(
    tmp_path: Path, contract
) -> None:
    contract = contract.model_copy(deep=True)
    contract.metrics = MetricsPolicy(
        hard_constraints={
            "invalid_solution_rate": MetricConstraint(max=0.0),
        },
        pareto_objectives={
            "raw_speedup": ObjectiveDirection.MAXIMIZE,
            "invalid_solution_rate": ObjectiveDirection.MINIMIZE,
        },
    )
    contract.required_controls = ["candidate_valid", "development_only"]
    contract.lock = ContractLock(
        content_sha256=sha256_object(
            contract.model_dump(mode="python", exclude={"lock"})
        )
    )

    def evaluate(path, seeds, repeats):
        del repeats
        value = Path(path).read_text()
        correct = "INVALID" not in value
        speedup = 2.0 if "FAST" in value and correct else 0.5 if correct else 0.0
        return {
            "correct": correct,
            "valid_rate": 1.0 if correct else 0.0,
            "raw_speedup": speedup,
            "instance_count": len(list(seeds)),
            "elapsed_seconds": 0.01,
            "failure": "" if correct else "INVALID_SOLUTION",
        }

    adapter = SetCoverStagedAdapter(
        contract=contract,
        policy=SetCoverFunnelPolicy(
            incumbent_speedup=1.5,
            probe_min_speedup=1.0,
        ),
        evaluator=evaluate,
        structural_check=lambda _ticket, _item: True,
    )
    fast = _materialized(tmp_path, contract, "FAST")
    ticket = _ticket(fast.item.acquisition.candidate.candidate_id)
    l0 = adapter.l0(ticket, fast)
    l1 = adapter.l1(ticket, fast, l0)
    full = adapter.full_evaluation(fast)

    assert l0.scientific_outcome is ScientificOutcome.NOT_EVALUABLE_DATA
    assert l0.data_eligible is False
    assert l1.stage is FunnelStage.L1
    assert l1.status is StageStatus.PASS
    assert l1.continue_pipeline
    assert full.evaluation.controls == {
        "candidate_valid": True,
        "development_only": True,
    }
    assert full.evaluation.metrics["raw_speedup"] == 2.0
    assert adapter.promotion_worthy(full)
    assert adapter.structural_transition_pass(ticket, fast)

    slow = _materialized(tmp_path, contract, "SLOW")
    slow_ticket = _ticket(slow.item.acquisition.candidate.candidate_id)
    slow_l0 = adapter.l0(slow_ticket, slow)
    slow_l1 = adapter.l1(slow_ticket, slow, slow_l0)
    assert slow_l1.status is StageStatus.BLOCK
    assert slow_l1.scientific_outcome is ScientificOutcome.VALID_NEGATIVE

    invalid = _materialized(tmp_path, contract, "INVALID")
    invalid_ticket = _ticket(invalid.item.acquisition.candidate.candidate_id)
    invalid_l0 = adapter.l0(invalid_ticket, invalid)
    assert invalid_l0.status is StageStatus.BLOCK
    assert (
        invalid_l0.scientific_outcome
        is ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
    )
    assert invalid_l0.reason_codes == [
        "SYNTHETIC_MECHANICS_FAIL",
        "EVALUATOR_INVALID_SOLUTION",
    ]

    def invalid_only_on_development(path, seeds, repeats):
        del path, repeats
        values = list(seeds)
        correct = all(seed >= 10_000 for seed in values)
        return {
            "correct": correct,
            "valid_rate": 1.0 if correct else 0.75,
            "raw_speedup": 2.0 if correct else 0.0,
            "instance_count": len(values),
            "elapsed_seconds": 0.01,
            "failure": "" if correct else "INVALID_SOLUTION:development",
        }

    stage_sensitive = SetCoverStagedAdapter(
        contract=contract,
        policy=SetCoverFunnelPolicy(
            incumbent_speedup=1.5,
            probe_min_speedup=1.0,
        ),
        evaluator=invalid_only_on_development,
    )
    stage_l0 = stage_sensitive.l0(ticket, fast)
    stage_l1 = stage_sensitive.l1(ticket, fast, stage_l0)
    assert stage_l1.mechanics_status.value == "FAIL"
    assert stage_l1.data_eligible is False
    assert (
        stage_l1.scientific_outcome
        is ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
    )


def test_set_cover_funnel_rejects_probe_or_mechanics_sampling_drift() -> None:
    with pytest.raises(ValueError, match="cannot overlap development"):
        SetCoverFunnelPolicy(
            incumbent_speedup=1.0,
            mechanics_seeds=[0],
        )
    with pytest.raises(ValueError, match="fixed subset"):
        SetCoverFunnelPolicy(
            incumbent_speedup=1.0,
            probe_seeds=[999],
        )


def test_structural_audit_ignores_literal_tuning_but_accepts_shape_change(
    tmp_path: Path, contract
) -> None:
    repo = tmp_path / "repo"
    source = repo / "tasks/algotune_set_cover/initial.py"
    source.parent.mkdir(parents=True)
    source.write_text("def solve(x):\n    return x[:2]\n", encoding="utf-8")
    _git(repo, "init", "-b", "master")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "parent",
    )
    parent = _git(repo, "rev-parse", "HEAD")
    original = _materialized(tmp_path, contract, "STRUCTURAL")
    candidate = original.item.acquisition.candidate.model_copy(
        update={"family": "new-family"}
    )
    item = MaterializedCandidate(
        generation_id=original.generation_id,
        item=original.item.model_copy(
            update={
                "acquisition": original.item.acquisition.model_copy(
                    update={"candidate": candidate}
                )
            }
        ),
        worktree=original.worktree,
        changed_files=original.changed_files,
        genetic_parent_id="PARENT",
        genetic_parent_commit=parent,
        candidate_commit=original.candidate_commit,
        candidate_ref=original.candidate_ref,
        patch_sha256=original.patch_sha256,
        parent_patch_sha256=original.parent_patch_sha256,
    )
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / f"{candidate.candidate_id}.escape_plan.json").write_text(
        json.dumps(
            {
                "replacement_mechanism": (
                    "minimum residual incidence pivot branching"
                )
            }
        ),
        encoding="utf-8",
    )
    audit = SetCoverStructuralTransitionAudit(
        repo_root=repo,
        parent_families={"PARENT": "old-family"},
        operator_plan_dir=plans,
    )
    ticket = _ticket(candidate.candidate_id)
    candidate_source = item.worktree / "tasks/algotune_set_cover/initial.py"
    candidate_source.write_text("def solve(x):\n    return x[:9]\n", encoding="utf-8")
    assert not audit(ticket, item)
    candidate_source.write_text(
        "def solve(x):\n"
        "    out = []\n"
        "    for value in x:\n"
        "        out.append(value)\n"
        "    return out\n",
        encoding="utf-8",
    )
    assert audit(ticket, item)
    assert audit.root_key(ticket, item) == "constraint_incidence_branching"
