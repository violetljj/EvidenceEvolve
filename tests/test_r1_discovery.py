from __future__ import annotations

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.discovery.campaign import (
    CampaignCandidate,
    CampaignRunner,
    EvaluationRun,
)
from evidence_evolve.governance.closure_registry import ClosureEntry, ClosureRegistry
from evidence_evolve.meta_evolution.policy import (
    AcquisitionSignals,
    CandidateAcquisition,
    ResearchPolicyGenome,
    rank_candidates,
)
from evidence_evolve.meta_evolution.promotion import (
    PolicyBenchmarkResult,
    PolicyPromotionDecision,
    PolicyPromotionProtocol,
    evaluate_policy_promotion,
)
from evidence_evolve.models import (
    EvaluationInput,
    MechanicsStatus,
    ResearchStage,
    ScientificOutcome,
    SearchDisposition,
)
from evidence_evolve.understanding.signatures import MechanismSupport


def _signals(**updates: float) -> AcquisitionSignals:
    values = {
        "admit_probability": 0.5,
        "expected_improvement": 0.2,
        "information_gain": 0.5,
        "novelty": 0.5,
        "estimated_cost": 0.1,
    }
    values.update(updates)
    return AcquisitionSignals(**values)


def test_closure_cannot_be_outscored(candidate) -> None:
    policy = ResearchPolicyGenome(policy_id="POLICY-R1")
    open_candidate = candidate.model_copy(update={"candidate_id": "OPEN-CANDIDATE"})
    closed_candidate = candidate.model_copy(
        update={"candidate_id": "CLOSED-CANDIDATE", "family": "closed_family"}
    )
    registry = ClosureRegistry(
        closures=[
            ClosureEntry(
                closure_id="CLOSE-1",
                family="closed_family",
                status="CLOSED",
                reopen_conditions=["EXTERNAL_EVIDENCE"],
                scope="test",
            )
        ]
    )
    decisions = rank_candidates(
        policy=policy,
        candidates=[
            CandidateAcquisition(candidate=open_candidate, signals=_signals()),
            CandidateAcquisition(
                candidate=closed_candidate,
                signals=_signals(
                    admit_probability=1.0,
                    expected_improvement=100.0,
                    information_gain=1.0,
                    novelty=1.0,
                    estimated_cost=0.0,
                ),
            ),
        ],
        closure_registry=registry,
    )
    assert decisions[0].candidate_id == "OPEN-CANDIDATE"
    assert decisions[0].eligible
    closed = next(item for item in decisions if item.candidate_id == "CLOSED-CANDIDATE")
    assert not closed.eligible
    assert closed.acquisition_score is None
    assert closed.reasons == ["CLOSED_FAMILY:CLOSE-1"]


def test_campaign_runner_gates_then_records_mechanism_and_resumes(
    tmp_path, contract, candidate
) -> None:
    candidate = candidate.model_copy(
        update={
            "ablation_plan": ["remove_representation"],
            "estimated_information_value": 0.8,
        }
    )
    item = CampaignCandidate(
        acquisition=CandidateAcquisition(candidate=candidate, signals=_signals()),
        stage=ResearchStage.M0_MECHANICS,
        reference_metrics={
            "clearance_mae_delta": 0.0,
            "false_block_delta_pp": 0.0,
        },
    )
    runner = CampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-R1"),
        run_dir=tmp_path / "run",
    )
    calls = 0

    def evaluate(scheduled: CampaignCandidate) -> EvaluationRun:
        nonlocal calls
        calls += 1
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=contract.lock.content_sha256,
                candidate=scheduled.acquisition.candidate,
                stage=scheduled.stage,
                mechanics_status=MechanicsStatus.PASS,
                data_eligible=True,
                metrics={
                    "clearance_mae_delta": -0.1,
                    "false_block_delta_pp": 0.0,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["python", "frozen_evaluator.py"],
            elapsed_seconds=0.01,
            ablation_results={"remove_representation": True},
        )

    result = runner.run_generation(
        generation_id="GEN-001",
        candidates=[item],
        evaluate=evaluate,
    )
    assert calls == 1
    assert result.evaluations[0].verdict.decision.value == "ADMIT"
    assert (
        result.evaluations[0].mechanism.support
        is MechanismSupport.INTERVENTION_SUPPORTED
    )
    assert result.evaluations[0].mechanism.authority == "SCHEDULING_ONLY"

    resumed = runner.run_generation(
        generation_id="GEN-001",
        candidates=[item],
        evaluate=evaluate,
    )
    assert calls == 1
    assert resumed.evaluations[0].resumed
    summary = ArchiveStore(tmp_path / "run" / "research.db").summary()
    assert summary["total"] == 1
    assert summary["scheduled_candidates"] == 1
    assert summary["by_mechanism_support"] == {"INTERVENTION_SUPPORTED": 1}
    memory = ArchiveStore(tmp_path / "run" / "research.db").scientific_memory()
    assert memory[0]["hypothesis"] == candidate.hypothesis
    assert memory[0]["scientific_outcome"] == "POSITIVE_HEADROOM"
    assert memory[0]["mechanism_support"] == "INTERVENTION_SUPPORTED"


def test_missing_reference_keeps_mechanism_inconclusive(
    tmp_path, contract, candidate
) -> None:
    item = CampaignCandidate(
        acquisition=CandidateAcquisition(candidate=candidate, signals=_signals()),
        stage=ResearchStage.M0_MECHANICS,
        reference_metrics={"false_block_delta_pp": 0.0},
    )
    runner = CampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-R1"),
        run_dir=tmp_path / "run",
    )

    def evaluate(scheduled: CampaignCandidate) -> EvaluationRun:
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=contract.lock.content_sha256,
                candidate=scheduled.acquisition.candidate,
                stage=scheduled.stage,
                mechanics_status=MechanicsStatus.PASS,
                data_eligible=True,
                metrics={
                    "clearance_mae_delta": -0.1,
                    "false_block_delta_pp": 0.0,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["python", "frozen_evaluator.py"],
            elapsed_seconds=0.01,
        )

    result = runner.run_generation(
        generation_id="GEN-MISSING-REFERENCE",
        candidates=[item],
        evaluate=evaluate,
    )
    assert result.evaluations[0].mechanism.support is MechanismSupport.INCONCLUSIVE
    assert (
        result.evaluations[0]
        .mechanism.signature_checks["clearance_mae_delta"]
        .reason
        == "REFERENCE_METRIC_MISSING"
    )


def test_one_candidate_failure_does_not_abort_the_generation(
    tmp_path, contract, candidate
) -> None:
    broken = candidate.model_copy(update={"candidate_id": "BROKEN-CANDIDATE"})
    working = candidate.model_copy(update={"candidate_id": "WORKING-CANDIDATE"})
    candidates = [
        CampaignCandidate(
            acquisition=CandidateAcquisition(candidate=item, signals=_signals()),
            stage=ResearchStage.M0_MECHANICS,
        )
        for item in (broken, working)
    ]
    runner = CampaignRunner(
        contract=contract.model_copy(
            update={
                "budgets": contract.budgets.model_copy(
                    update={"proposal_calls": 2, "mechanics_runs": 2}
                )
            }
        ),
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-FAILURE-ISOLATION"),
        run_dir=tmp_path / "run",
    )

    def evaluate(scheduled: CampaignCandidate) -> EvaluationRun:
        current = scheduled.acquisition.candidate
        if current.candidate_id == "BROKEN-CANDIDATE":
            raise RuntimeError("candidate-local implementation error")
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=contract.lock.content_sha256,
                candidate=current,
                stage=scheduled.stage,
                mechanics_status=MechanicsStatus.PASS,
                data_eligible=True,
                metrics={
                    "clearance_mae_delta": -0.1,
                    "false_block_delta_pp": 0.0,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["fake-evaluator"],
            elapsed_seconds=0.01,
        )

    result = runner.run_generation(
        generation_id="GEN-FAILURE-ISOLATION",
        candidates=candidates,
        evaluate=evaluate,
        max_workers=2,
    )
    assert [item.candidate_id for item in result.failures] == ["BROKEN-CANDIDATE"]
    assert [item.candidate_id for item in result.evaluations] == ["WORKING-CANDIDATE"]
    assert result.evaluations[0].search_disposition is SearchDisposition.CODE_PARENT


def _policy_benchmark(policy_id: str, **updates: object) -> PolicyBenchmarkResult:
    values: dict[str, object] = {
        "policy_id": policy_id,
        "suite_id": "CHRONO-HELDOUT-1",
        "task_count": 8,
        "held_out": True,
        "blind": True,
        "blind_breakthrough_rate": 0.2,
        "valid_improvement_per_cost": 0.1,
        "hypothesis_calibration": 0.7,
        "mechanism_prediction_accuracy": 0.6,
        "redundant_experiment_rate": 0.2,
        "closure_violations": 0,
        "fresh_set_robustness": 0.8,
        "reproducibility_rate": 1.0,
    }
    values.update(updates)
    return PolicyBenchmarkResult(**values)


def test_policy_promotion_requires_blind_heldout_progress_and_human_review() -> None:
    baseline = _policy_benchmark("POLICY-BASE")
    candidate = _policy_benchmark(
        "POLICY-CANDIDATE",
        blind_breakthrough_rate=0.3,
        valid_improvement_per_cost=0.15,
    )
    verdict = evaluate_policy_promotion(
        candidate=candidate,
        baseline=baseline,
        protocol=PolicyPromotionProtocol(
            min_tasks=5,
            min_fresh_set_robustness=0.75,
        ),
    )
    assert verdict.decision is PolicyPromotionDecision.ELIGIBLE_FOR_HUMAN_PROMOTION
    assert verdict.final_promotion_requires_human
    assert verdict.authority == "META_EVALUATION_ONLY"


def test_policy_promotion_holds_closure_violations() -> None:
    verdict = evaluate_policy_promotion(
        candidate=_policy_benchmark(
            "POLICY-CANDIDATE",
            blind_breakthrough_rate=0.9,
            closure_violations=1,
        ),
        baseline=_policy_benchmark("POLICY-BASE"),
        protocol=PolicyPromotionProtocol(),
    )
    assert verdict.decision is PolicyPromotionDecision.HOLD
    assert "CLOSURE_VIOLATIONS_PRESENT" in verdict.reasons
