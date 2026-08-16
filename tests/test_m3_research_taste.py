from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_evolve.benchmarks.algotune_set_cover_m3_r0 import (
    evaluate_candidate_profiled_with_timeout,
)
from evidence_evolve.discovery.m3_research_taste import (
    M3CandidateObservation,
    M3StructuralEscapeStagedAdapter,
    MechanismAncestryDetector,
    MechanismFirstPlan,
    PlannerArm,
    check_mechanism_contract,
    load_m3_policy,
    planner_treatment_prompt,
    score_research_taste,
    summarize_m3,
)
from evidence_evolve.discovery.throughput import CandidateTicket, StageStatus
from evidence_evolve.models import ScientificOutcome


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = (
    REPO_ROOT / "research/policies/algotune_set_cover_m3_r0_research_taste_v0.yaml"
)
RESULT_PATH = (
    REPO_ROOT / "research/results/algotune_set_cover_m3_r0_research_taste_v0/result.json"
)


def _policy():
    return load_m3_policy(POLICY_PATH)


def _closed_source(path: Path) -> Path:
    path.write_text(
        "class Solver:\n"
        "    def solve(self, problem):\n"
        "        return self.inherited_solver(problem)\n"
        "    def inherited_solver(self, problem):\n"
        "        total = 0\n"
        "        for subset in problem:\n"
        "            total += len(subset)\n"
        "        return [total]\n",
        encoding="utf-8",
    )
    return path


def _plan(arm: PlannerArm = PlannerArm.RESEARCH_TASTE) -> MechanismFirstPlan:
    return MechanismFirstPlan(
        candidate_id="M3-C01",
        arm=arm,
        old_mechanism="pivot branch and bound",
        removed_assumptions=["delete inherited_solver fallback"],
        deleted_components=["inherited_solver"],
        new_computational_primitives=["integer linear programming"],
        new_state_representation="cover inequalities",
        new_solver_pipeline=["formulate", "solve exactly"],
        expected_complexity_advantage="avoid enumerating the old residual tree",
        end_to_end_latency_thesis="compiled propagation reduces measured wall time",
        predicted_failure_mode="formulation overhead dominates",
        information_gain_if_failed="isolates formulation cost",
        forbidden_dependencies=["inherited_solver"],
        contrarian_thesis=(
            "delete branching and delegate exact search to propagation"
            if arm is PlannerArm.RESEARCH_TASTE
            else None
        ),
    )


def test_m3_policy_freezes_paired_three_arm_admission() -> None:
    policy = _policy()
    assert [item.arm for item in policy.arms] == list(PlannerArm)
    assert all(item.proposal_budget == 8 for item in policy.arms)
    assert policy.admission_target == 5
    assert policy.evaluator_timeout_seconds == 60
    assert policy.l0_violation_outcome is ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
    assert policy.arms[-1].contrarian_slots == [2, 4, 6, 8]
    assert all(item.lineage_hard_block for item in policy.arms)


def test_m3_result_does_not_overclaim_research_taste_advantage() -> None:
    import json

    result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    assert result["interpretation_status"] == "RESEARCH_TASTE_ADVANTAGE_NOT_DEMONSTRATED"
    assert result["interpretation_is_not_scientific_outcome"] is True
    assert result["conversion_claim_authorized"] is False
    assert result["confirmation_runs"] == 0
    arms = {item["arm"]: item for item in result["arm_summaries"]}
    assert arms["RESEARCH_TASTE"]["genuine_structural_roots"] == 8
    assert arms["R4_BASELINE"]["genuine_structural_roots"] == 8
    assert arms["RESEARCH_TASTE"]["exact_valid_roots"] < arms["R4_BASELINE"][
        "exact_valid_roots"
    ]


def test_planner_treatment_isolates_contract_memory_and_contrarian_policy() -> None:
    policy = _policy()
    baseline = planner_treatment_prompt(policy, PlannerArm.R4_BASELINE, 2)
    contract = planner_treatment_prompt(policy, PlannerArm.MECHANISM_CONTRACT, 2)
    taste = planner_treatment_prompt(policy, PlannerArm.RESEARCH_TASTE, 2)
    assert "MechanismFirstPlan" not in baseline
    assert "MechanismFirstPlan" in contract
    assert "Frozen negative-taste memory" not in contract
    assert "Frozen negative-taste memory" in taste
    assert "Contrarian slot" in taste
    assert "Planner scheduling reward" in taste


def test_mechanism_first_contract_requires_explicit_closed_core_deletion() -> None:
    policy = _policy()
    assert check_mechanism_contract(_plan(), policy).passed
    broken = _plan().model_copy(update={"deleted_components": ["some cache"]})
    decision = check_mechanism_contract(broken, policy)
    assert not decision.passed
    assert "CLOSED_CORE_DELETION_UNSPECIFIED" in decision.reason_codes


def test_ancestry_detector_catches_direct_and_renamed_closed_core(tmp_path: Path) -> None:
    detector = MechanismAncestryDetector(
        policy=_policy(),
        closed_source=_closed_source(tmp_path / "closed.py"),
        report_dir=tmp_path / "reports",
    )
    direct = tmp_path / "direct.py"
    direct.write_text(
        "class Solver:\n"
        "    def solve(self, problem): return self.inherited_solver(problem)\n"
        "    def inherited_solver(self, problem): return []\n",
        encoding="utf-8",
    )
    report = detector.assess("M3-DIRECT", direct)
    assert report.lineage_retained
    assert "inherited_solver" in report.direct_closed_markers
    assert report.reason_codes == ["STRUCTURAL_ESCAPE_VIOLATION"]

    renamed = tmp_path / "renamed.py"
    renamed.write_text(
        "class Solver:\n"
        "    def solve(self, problem): return self.fresh_name(problem)\n"
        "    def fresh_name(self, data):\n"
        "        answer = 0\n"
        "        for item in data:\n"
        "            answer += len(item)\n"
        "        return [answer]\n",
        encoding="utf-8",
    )
    report = detector.assess("M3-RENAMED", renamed)
    assert report.lineage_retained
    assert report.matched_closed_core_fingerprints == {
        "fresh_name": "inherited_solver"
    }
    assert (tmp_path / "reports/M3-RENAMED.mechanism_ancestry.json").is_file()


def test_ancestry_detector_accepts_new_solver_ownership(tmp_path: Path) -> None:
    detector = MechanismAncestryDetector(
        policy=_policy(), closed_source=_closed_source(tmp_path / "closed.py")
    )
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "class Solver:\n"
        "    def solve(self, problem):\n"
        "        clauses = {x for subset in problem for x in subset}\n"
        "        return [] if clauses else []\n",
        encoding="utf-8",
    )
    report = detector.assess("M3-NEW", candidate)
    assert report.structural_escape_pass
    assert not report.lineage_retained
    score = score_research_taste(
        _plan(), report, prior_primitive_signatures=[["pivot branch and bound"]]
    )
    assert score.implementation_fidelity == 1.0
    assert score.primitive_novelty == 1.0
    assert score.performance_excluded is True


def test_ancestry_detector_blocks_closed_state_and_pipeline_shape(tmp_path: Path) -> None:
    detector = MechanismAncestryDetector(
        policy=_policy(), closed_source=_closed_source(tmp_path / "closed.py")
    )
    candidate = tmp_path / "shape.py"
    candidate.write_text(
        "class Solver:\n"
        "    def solve(self, problem):\n"
        "        uncovered_mask = 0\n"
        "        return self.branch(self.bound(uncovered_mask))\n"
        "    def branch(self, state): return []\n"
        "    def bound(self, state): return state\n",
        encoding="utf-8",
    )
    report = detector.assess("M3-SHAPE", candidate)
    assert report.state_pipeline_lineage_match
    assert report.lineage_retained
    assert report.reason_codes == ["STRUCTURAL_ESCAPE_VIOLATION"]


class _FakeProfiledAdapter:
    policy = SimpleNamespace(mechanics_seeds=[1], mechanics_repeats=1)

    def __init__(self, raw):
        self.raw = raw
        self.run_calls = 0

    def _run(self, item, *, seeds, repeats):
        del item, seeds, repeats
        self.run_calls += 1
        return dict(self.raw)

    def candidate_path(self, item):
        return item.worktree / "tasks/algotune_set_cover/initial.py"

    def _profile_metrics(self, raw):
        del raw
        return {"wall_time_ns": 1.0}

    def _failure_reason(self, raw):
        del raw
        return None


def test_m3_l0_maps_lineage_retention_to_existing_invalid_outcome(tmp_path: Path) -> None:
    detector = MechanismAncestryDetector(
        policy=_policy(), closed_source=_closed_source(tmp_path / "closed.py")
    )
    worktree = tmp_path / "worktree"
    candidate = worktree / "tasks/algotune_set_cover/initial.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text(
        "class Solver:\n"
        "    def inherited_solver(self, problem): return []\n"
        "    def solve(self, problem): return self.inherited_solver(problem)\n",
        encoding="utf-8",
    )
    raw = {
        "correct": True,
        "adapter_exception": False,
        "telemetry_available": True,
        "valid_rate": 1.0,
        "elapsed_seconds": 0.1,
    }
    profiled = _FakeProfiledAdapter(raw)
    adapter = M3StructuralEscapeStagedAdapter(
        profiled_adapter=profiled, detector=detector
    )
    ticket = CandidateTicket(
        candidate_id="M3-C01",
        dispatch_index=1,
        lineage_id="M3",
        operator_class="new_solver",
        genetic_parent_id="ROOT",
        requires_structural_transition=True,
    )
    decision = adapter.l0(ticket, SimpleNamespace(worktree=worktree))
    assert decision.status is StageStatus.BLOCK
    assert decision.scientific_outcome is ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
    assert "STRUCTURAL_ESCAPE_VIOLATION" in decision.reason_codes
    assert profiled.run_calls == 0
    assert set(ScientificOutcome) == {
        ScientificOutcome.POSITIVE_HEADROOM,
        ScientificOutcome.VALID_NEGATIVE,
        ScientificOutcome.NOT_EVALUABLE_DATA,
        ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
    }


def test_m3_summary_uses_structural_roots_as_primary_and_speed_as_secondary() -> None:
    rows = []
    for arm in PlannerArm:
        for slot in range(1, 9):
            genuine = arm is PlannerArm.RESEARCH_TASTE and slot <= 5
            rows.append(
                M3CandidateObservation(
                    candidate_id=f"{arm.value}-{slot}",
                    arm=arm,
                    paired_slot=slot,
                    proposal_contract_pass=(
                        None if arm is PlannerArm.R4_BASELINE else True
                    ),
                    implementation_succeeded=True,
                    lineage_retained=not genuine,
                    exact_valid=genuine and slot <= 3,
                    basin_signature=[f"B{slot}"] if genuine else [],
                    token_count=1000,
                    scientific_outcome=(
                        ScientificOutcome.POSITIVE_HEADROOM
                        if genuine and slot <= 3
                        else ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
                    ),
                    raw_speedup=100.0 if genuine and slot <= 3 else None,
                )
            )
    summaries = {item.arm: item for item in summarize_m3(rows, _policy())}
    taste = summaries[PlannerArm.RESEARCH_TASTE]
    assert taste.genuine_structural_roots == 5
    assert taste.exact_valid_roots == 3
    assert taste.admission_target_met
    assert taste.best_exact_valid_speedup == 100.0
    assert not summaries[PlannerArm.R4_BASELINE].admission_target_met


def test_m3_evaluator_timeout_is_terminal_without_retry(tmp_path: Path) -> None:
    candidate = tmp_path / "slow.py"
    candidate.write_text(
        "import time\n"
        "class Solver:\n"
        "    def solve(self, problem):\n"
        "        time.sleep(1.0)\n"
        "        return []\n"
        "    def profile_snapshot(self): return {}\n",
        encoding="utf-8",
    )
    result = evaluate_candidate_profiled_with_timeout(
        candidate, [1], 1, timeout_seconds=0.05
    )
    assert not result["correct"]
    assert result["failure"].startswith("TIMEOUT:")
    assert result["adapter_exception"] is True


def test_contract_only_arm_rejects_contrarian_treatment() -> None:
    with pytest.raises(ValueError, match="cannot receive contrarian"):
        MechanismFirstPlan.model_validate(
            {
                **_plan().model_dump(mode="json"),
                "arm": PlannerArm.MECHANISM_CONTRACT,
            }
        )
