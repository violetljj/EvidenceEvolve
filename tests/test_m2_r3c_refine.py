from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evidence_evolve.benchmarks.algotune_set_cover_m2_r3c import _stage_feedback
from evidence_evolve.discovery.m2_r3c_refine import (
    BasinRefinementBrief,
    M2R3CPolicy,
    _trace_activation_chunk,
    run_exactness_canary,
)
from evidence_evolve.discovery.throughput import (
    CandidateFunnelRecord,
    CandidateTicket,
    FunnelDecision,
    FunnelStage,
    StageStatus,
)
from evidence_evolve.models import MechanicsStatus, ScientificOutcome


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = (
    REPO_ROOT
    / "research/policies/algotune_set_cover_m2_r3c_basin_refine_v0.yaml"
)


def _policy() -> M2R3CPolicy:
    return M2R3CPolicy.model_validate(
        yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    )


def _solver_source(*, fail_large_case: bool = False) -> str:
    failure = (
        "\n        if len(problem) > 20:\n            return list(range(5))"
        if fail_large_case
        else ""
    )
    return (
        "from tasks.algotune_set_cover.common import reference_solution\n\n"
        "class Solver:\n"
        "    def solve(self, problem):"
        f"{failure}\n"
        "        frozen = tuple(tuple(item) for item in problem)\n"
        "        return list(reference_solution(frozen))\n"
    )


def test_r3c_policy_isolates_one_active_basin_and_four_descendants() -> None:
    policy = _policy()

    assert policy.operator_class == "BASIN_REFINE_TELEMETRY"
    assert policy.candidate_slots == 4
    assert policy.early_stop_non_improving_attempts == 3
    assert [item.basin_id for item in policy.basins] == ["pivot_branch_and_bound"]
    assert policy.conversion_threshold == 47.801
    assert policy.hybrid_permitted is False
    assert policy.structural_escape_permitted is False

    payload = policy.model_dump(mode="python")
    payload["basins"] = payload["basins"] * 2
    with pytest.raises(ValueError):
        M2R3CPolicy.model_validate(payload)


def test_exactness_canary_passes_reference_and_blocks_known_r3_regression(
    tmp_path: Path,
) -> None:
    correct = tmp_path / "correct.py"
    correct.write_text(_solver_source(), encoding="utf-8")
    faulty = tmp_path / "faulty.py"
    faulty.write_text(_solver_source(fail_large_case=True), encoding="utf-8")

    passed = run_exactness_canary(correct)
    blocked = run_exactness_canary(faulty)

    assert passed.passed is True
    assert passed.cases_executed == 4506
    assert blocked.passed is False
    assert blocked.failed_case_id == "REGRESSION_R3_PIVOT_EXACTNESS_DEV_SEED_57"
    assert blocked.expected_cardinality == 4
    assert blocked.actual_cardinality == 5
    assert blocked.scientific_authority == "MECHANICS_ONLY"


def test_activation_trace_measures_primary_path_without_fallback(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "from tasks.algotune_set_cover.common import reference_solution\n\n"
        "class Solver:\n"
        "    def solve(self, problem):\n"
        "        def inherited_solver():\n"
        "            return []\n"
        "        def kernel_search():\n"
        "            return True\n"
        "        kernel_search()\n"
        "        frozen = tuple(tuple(item) for item in problem)\n"
        "        return list(reference_solution(frozen))\n",
        encoding="utf-8",
    )

    rows = _trace_activation_chunk(str(candidate), [0, 1])

    assert all(row["valid"] for row in rows)
    assert all(row["kernel_search_calls"] == 1 for row in rows)
    assert all(row["kernel_search_successes"] == 1 for row in rows)
    assert all(row["fallback_calls"] == 0 for row in rows)


def test_full_stage_feedback_preserves_l1_metrics_reasons_and_outcome() -> None:
    ticket = CandidateTicket(
        candidate_id="R3C-REFINE-01-C01",
        dispatch_index=1,
        lineage_id="pivot_branch_and_bound",
        operator_class="BASIN_REFINE_TELEMETRY",
        genetic_parent_id="R2-WAVE-001-C03",
    )
    l0 = FunnelDecision(
        stage=FunnelStage.L0,
        status=StageStatus.PASS,
        continue_pipeline=True,
        mechanics_status=MechanicsStatus.PASS,
        data_eligible=True,
        controls={"exactness_canary": True},
        metrics={"exactness_cases_executed": 4506.0},
        scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
        reason_codes=["L0_PASS"],
    )
    l1 = FunnelDecision(
        stage=FunnelStage.L1,
        status=StageStatus.BLOCK,
        mechanics_status=MechanicsStatus.PASS,
        data_eligible=True,
        controls={"valid": True},
        metrics={"raw_speedup": 12.5},
        scientific_outcome=ScientificOutcome.VALID_NEGATIVE,
        reason_codes=["BASIN_LOCAL_PROBE_BLOCK"],
    )
    record = CandidateFunnelRecord(
        ticket=ticket,
        decisions=[l0, l1],
        terminal_stage=FunnelStage.L1,
        terminal_status="BLOCKED",
        wall_seconds=0.1,
    )

    feedback = _stage_feedback(record)

    assert [item.stage for item in feedback] == ["PROPOSE", "IMPLEMENT", "L0", "L1"]
    assert feedback[-1].metrics == {"raw_speedup": 12.5}
    assert feedback[-1].reason_codes == ["BASIN_LOCAL_PROBE_BLOCK"]
    assert feedback[-1].scientific_outcome is ScientificOutcome.VALID_NEGATIVE


def test_decoded_brief_enforces_frozen_byte_bound() -> None:
    brief = BasinRefinementBrief(
        brief_id="a" * 16,
        candidate_id="R3C-REFINE-01-C01",
        basin_id="pivot_branch_and_bound",
        parent_candidate_id="R2-WAVE-001-C03",
        parent_score=38.7,
        available_counters={"fallback_rate": 0.0},
        regression_seed_ids=[57],
        improvement_seed_ids=[],
        parent_runtime_ns={"p50": 100, "p90": 200, "p99": 300},
        activation_gate={"parent_passed": True},
        required_plan_fields=["a", "b", "c", "d", "e"],
    )

    brief.assert_size(4096)
    with pytest.raises(ValueError, match="exceeds frozen size"):
        brief.assert_size(100)
