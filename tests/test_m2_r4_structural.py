from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from evidence_evolve.discovery.m2_r4_structural import (
    M2R4Policy,
    R4SourceAudit,
)
from tasks.algotune_set_cover.r4_profiling import evaluate_candidate_profiled


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = (
    REPO_ROOT
    / "research/policies/algotune_set_cover_m2_r4_structural_basin_wave_v0.yaml"
)


def _policy_payload() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def test_r4_policy_freezes_eight_distinct_nonpivot_roots() -> None:
    policy = M2R4Policy.model_validate(_policy_payload())
    assert len(policy.basins) == 8
    assert policy.probe_min_speedup == 45.0
    assert policy.conversion_threshold == 47.801
    assert policy.closure_interpretation == "PIVOT_BNB_LOCAL_REFINEMENT_NOT_SUPPORTED"
    assert {basin.basin_id for basin in policy.basins} == {
        "primal_dual_guided",
        "lagrangian_exact_repair",
        "reduction_primal_dual_exact",
        "meet_in_middle_decomposition",
        "lower_bound_redesign",
        "heuristic_then_exact",
        "online_branch_value",
        "state_equivalence_redesign",
    }


def test_r4_policy_rejects_signature_collapse() -> None:
    payload = _policy_payload()
    payload["basins"][1]["mechanism_signature"] = payload["basins"][0][
        "mechanism_signature"
    ]
    with pytest.raises(ValueError, match="overlap too strongly"):
        M2R4Policy.model_validate(payload)


def test_r4_result_keeps_scheduling_interpretation_separate_from_outcomes() -> None:
    result = json.loads(
        (
            REPO_ROOT
            / "research/results/algotune_set_cover_m2_r4_structural_basin_wave_v0/result.json"
        ).read_text(encoding="utf-8")
    )
    assert result["interpretation_status"] == "STRUCTURAL_ESCAPE_NOT_DEMONSTRATED"
    assert result["interpretation_is_not_scientific_outcome"] is True
    assert result["conversion_claim_authorized"] is False
    assert result["best_valid_l1"]["raw_speedup"] < result["probe_promotion_threshold"]
    assert result["posthoc_mechanics_audit"][
        "candidates_retaining_closed_inherited_solver"
    ] == 7


class _BaseAudit:
    def __call__(self, ticket, item) -> bool:
        del ticket, item
        return True


def test_r4_source_audit_rejects_pivot_lineage_and_accepts_profiled_root(
    tmp_path: Path,
) -> None:
    policy = M2R4Policy.model_validate(_policy_payload())
    candidate_id = "M2-R4-STRUCTURAL-001-C01"
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan = {
        "schema_version": "1.0",
        "candidate_id": candidate_id,
        "operator_class": "primal_dual_guided",
        "operator_directive": policy.basins[0].directive,
        "genetic_parent_id": "ROOT",
        "context_candidate_ids": ["ROOT"],
        "addressed_failure_candidate_ids": ["ROOT"],
        "preserved_mechanisms": ["input normalization"],
        "mechanism_to_replace": "pivot branch and bound",
        "replacement_mechanism": "primal dual exact residual",
        "target_family": "primal_dual_exact",
        "representation_change": "dual prices over uncovered elements",
        "solver_process_change": "dual ascent then exact residual correction",
        "integration_steps": ["replace inherited search"],
        "correctness_invariants": ["return an optimal cover"],
        "predicted_failure_mode": "dual bookkeeping can dominate latency",
        "falsifier": "external wall time misses the threshold",
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "basin_id": "primal_dual_guided",
        "mechanism_signature": policy.basins[0].mechanism_signature,
        "inherited_pivot_bnb_removed": True,
        "wall_clock_cost_hypothesis": "lower tail latency through dual pruning",
        "profiling_contract": policy.required_profile_metrics,
    }
    (plan_dir / f"{candidate_id}.escape_plan.json").write_text(
        json.dumps(plan), encoding="utf-8"
    )
    worktree = tmp_path / "worktree"
    source = worktree / "tasks/algotune_set_cover/initial.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Solver:\n"
        "    def profile_snapshot(self): return {}\n",
        encoding="utf-8",
    )
    ticket = SimpleNamespace(
        candidate_id=candidate_id, operator_class="primal_dual_guided"
    )
    item = SimpleNamespace(worktree=worktree)
    audit = R4SourceAudit(
        base_audit=_BaseAudit(),
        policy=policy,
        operator_plan_dir=plan_dir,
    )
    assert audit(ticket, item)
    source.write_text(
        "def kernel_search(): pass\n"
        "class Solver:\n"
        "    def profile_snapshot(self): return {}\n",
        encoding="utf-8",
    )
    assert not audit(ticket, item)


def test_profiled_evaluator_records_external_latency_and_diagnostic_counters(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "from tasks.algotune_set_cover.common import solve_reference\n"
        "class Solver:\n"
        "    def solve(self, problem):\n"
        "        return list(solve_reference(tuple(tuple(x) for x in problem)))\n"
        "    def profile_snapshot(self):\n"
        "        return {'node_expansions': 2, 'bound_time_ns': 3, "
        "'cache_time_ns': 4, 'reduction_ratio': 0.6}\n",
        encoding="utf-8",
    )
    result = evaluate_candidate_profiled(candidate, [10_000, 10_001], 1, problem_size=8)
    assert result["correct"]
    assert result["wall_time_p50_ns"] > 0
    assert result["wall_time_p95_ns"] >= result["wall_time_p50_ns"]
    assert result["wall_time_p99_ns"] >= result["wall_time_p95_ns"]
    assert result["node_expansions"] == 4.0
    assert result["reduction_ratio"] == pytest.approx(0.6)
    assert result["telemetry_scientific_authority"] == "NONE_DIAGNOSTIC_ONLY"
