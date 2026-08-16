from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evidence_evolve.discovery.m2_r3_refine import (
    BasinAttempt,
    BasinRetentionAudit,
    BasinRuntimeProfile,
    BasinState,
    InstanceTiming,
    M2R3Policy,
    allocate_adaptive_slots,
    compare_profiles,
)
from evidence_evolve.discovery.throughput import CandidateTicket


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = (
    REPO_ROOT
    / "research/policies/algotune_set_cover_m2_r3_basin_refine_v0.yaml"
)


def _policy() -> M2R3Policy:
    return M2R3Policy.model_validate(
        yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    )


def _attempt(index: int, *, improved: bool, score: float | None) -> BasinAttempt:
    return BasinAttempt(
        candidate_id=f"R3-WAVE-{index:02d}-C01",
        parent_candidate_id="ROOT-C01",
        wave_id=f"R3-WAVE-{index:02d}",
        score=score,
        dev_valid=score is not None,
        basin_retained=True,
        improved_local_incumbent=improved,
        terminal_stage="L2",
        terminal_status="COMPLETE",
    )


def _state(
    basin_id: str,
    *,
    root: float,
    best: float,
    attempts: int,
    stagnant: int,
) -> BasinState:
    return BasinState(
        basin_id=basin_id,
        root_candidate_id=f"{basin_id}-ROOT",
        local_incumbent_id=f"{basin_id}-BEST",
        root_score=root,
        local_incumbent_score=best,
        attempts=[
            _attempt(index, improved=index == attempts and best > root, score=best)
            for index in range(1, attempts + 1)
        ],
        consecutive_non_improving=stagnant,
    )


def _profile(candidate_id: str, times: list[int]) -> BasinRuntimeProfile:
    instances = [
        InstanceTiming(
            seed=index,
            valid=True,
            candidate_time_ns=value,
            reference_time_ns=1_000,
            speedup=1_000 / value,
        )
        for index, value in enumerate(times)
    ]
    ordered = sorted(times)
    return BasinRuntimeProfile(
        candidate_id=candidate_id,
        candidate_commit="a" * 40,
        instances=instances,
        valid_instances=len(instances),
        aggregate_speedup=len(times) * 1_000 / sum(times),
        candidate_time_p50_ns=ordered[len(ordered) // 2],
        candidate_time_p90_ns=ordered[-1],
        candidate_time_p99_ns=ordered[-1],
        worker_count=1,
        wall_seconds=0.1,
    )


def test_m2_r3_policy_freezes_primary_conversion_endpoint() -> None:
    policy = _policy()

    primary = [item for item in policy.basins if item.primary_endpoint]
    assert len(primary) == 1
    assert primary[0].basin_id == "pivot_branch_and_bound"
    assert policy.conversion_threshold == 47.801
    assert policy.total_proposal_slots == 16
    assert policy.hybrid_permitted is False
    assert policy.structural_escape_permitted is False

    payload = policy.model_dump(mode="python")
    payload["conversion_threshold"] = primary[0].root_score
    with pytest.raises(ValueError, match="conversion threshold"):
        M2R3Policy.model_validate(payload)


def test_adaptive_slots_follow_slope_and_stop_stagnant_basins() -> None:
    policy = _policy()
    states = {
        "fast": _state("fast", root=20.0, best=32.0, attempts=3, stagnant=0),
        "medium": _state("medium", root=20.0, best=26.0, attempts=3, stagnant=0),
        "stopped": _state("stopped", root=20.0, best=20.0, attempts=3, stagnant=3),
        "slow": _state("slow", root=20.0, best=23.0, attempts=3, stagnant=1),
    }

    assert allocate_adaptive_slots(states, policy) == [
        "fast",
        "medium",
        "slow",
        "fast",
    ]


def test_profile_comparison_exposes_instance_wins_losses_and_tails() -> None:
    parent = _profile("PARENT", [100, 100, 100, 100])
    child = _profile("CHILD", [50, 120, 100, 80])

    comparison = compare_profiles(parent, child)

    assert comparison["winning_seed_ids"] == [0, 3]
    assert comparison["losing_seed_ids"] == [1]
    assert comparison["tie_count"] == 1
    assert comparison["parent_runtime_ns"]["p90"] == 100
    assert comparison["child_runtime_ns"]["p90"] == 120
    assert comparison["scientific_authority"] == "NONE_SCHEDULING_ONLY"


def test_basin_retention_audit_blocks_missing_mechanism_anchors(tmp_path: Path) -> None:
    policy = _policy()
    audit = BasinRetentionAudit(policy)
    ticket = CandidateTicket(
        candidate_id="R3-REFINE-01-C01",
        dispatch_index=1,
        lineage_id="pivot_branch_and_bound",
        operator_class="BASIN_REFINE",
        genetic_parent_id="R2-WAVE-001-C03",
    )
    source = tmp_path / "candidate.py"
    source.write_text("def kernel_search():\n    pivot_sets = []\n", encoding="utf-8")
    assert audit(ticket, source) is True

    source.write_text("def unrelated_solver():\n    return []\n", encoding="utf-8")
    assert audit(ticket, source) is False
