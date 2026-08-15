from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.discovery.campaign import CampaignCandidate, CampaignRunner, EvaluationRun
from evidence_evolve.discovery.director import ResearchAction, ResearchDirector
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.meta_evolution.policy import (
    AcquisitionSignals,
    CandidateAcquisition,
    ResearchPolicyGenome,
)
from evidence_evolve.models import (
    EvaluationInput,
    MechanicsStatus,
    ResearchStage,
    ScientificOutcome,
)
from evidence_evolve.research_memory import (
    MemoryEpistemics,
    MemoryKind,
    MemoryRole,
)


def _run_negative_with_open_question(tmp_path, contract, candidate) -> ArchiveStore:
    candidate = candidate.model_copy(
        update={
            "ablation_plan": ["remove_representation"],
            "mechanism_claims": ["representation separates the dominant error mode"],
            "assumptions": ["the development distribution exposes the error mode"],
            "behavior_descriptor": {"regime": "long_horizon"},
        }
    )
    item = CampaignCandidate(
        acquisition=CandidateAcquisition(
            candidate=candidate,
            signals=AcquisitionSignals(
                admit_probability=0.5,
                expected_improvement=0.2,
                information_gain=0.8,
                novelty=0.7,
                estimated_cost=0.1,
            ),
        ),
        stage=ResearchStage.M0_MECHANICS,
        reference_metrics={
            "clearance_mae_delta": 0.0,
            "false_block_delta_pp": 0.0,
        },
    )
    runner = CampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-MEMORY-V2"),
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
                    "false_block_delta_pp": 0.2,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["python", "frozen_evaluator.py"],
            elapsed_seconds=0.01,
        )

    result = runner.run_generation(
        generation_id="GEN-MEMORY-001",
        candidates=[item],
        evaluate=evaluate,
    )
    assert result.evaluations[0].verdict.scientific_outcome is ScientificOutcome.VALID_NEGATIVE
    return ArchiveStore(tmp_path / "run" / "research.db")


def test_memory_v2_compiles_practical_cards_and_enforces_role_firewall(
    tmp_path, contract, candidate
) -> None:
    archive = _run_negative_with_open_question(tmp_path, contract, candidate)

    explorer = archive.research_memory_packet(
        role=MemoryRole.HYPOTHESIS_EXPLORER,
        query="representation long horizon",
        campaign=contract.campaign.id,
        limit=12,
    )
    assert {card.kind for card in explorer.cards} == {
        MemoryKind.RESULT,
        MemoryKind.FAILURE,
        MemoryKind.MECHANISM,
        MemoryKind.LINEAGE,
        MemoryKind.FRONTIER,
    }
    assert all(card.epistemics.authority == "SCHEDULING_ONLY" for card in explorer.cards)
    assert all(
        card.provenance.receipt_ids == ["test_campaign:GEN-MEMORY-001:TEST-CANDIDATE-001:M0_MECHANICS"]
        for card in explorer.cards
    )
    assert all(card.scope.stage is ResearchStage.M0_MECHANICS for card in explorer.cards)

    implementer = archive.research_memory_packet(
        role=MemoryRole.IMPLEMENTER,
        campaign=contract.campaign.id,
        limit=12,
    )
    assert {card.kind for card in implementer.cards} == {
        MemoryKind.FAILURE,
        MemoryKind.LINEAGE,
        MemoryKind.PROCEDURE,
    }
    procedure = next(card for card in implementer.cards if card.kind is MemoryKind.PROCEDURE)
    assert "frozen_evaluator.py" in " ".join(procedure.content.procedure)

    director = archive.research_memory_packet(
        role=MemoryRole.RESEARCH_DIRECTOR,
        campaign=contract.campaign.id,
        limit=12,
    )
    assert {card.kind for card in director.cards} == set(MemoryKind) - {
        MemoryKind.TRANSFER
    }

    gate = archive.research_memory_packet(
        role=MemoryRole.GATE_ENGINE,
        campaign=contract.campaign.id,
    )
    assert gate.cards == []
    with sqlite3.connect(archive.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_retrieval_events"
        ).fetchone()[0] == 4


def test_memory_changes_director_action_and_cannot_escalate_authority(
    tmp_path, contract, candidate
) -> None:
    archive = _run_negative_with_open_question(tmp_path, contract, candidate)
    packet = archive.research_memory_packet(
        role=MemoryRole.HYPOTHESIS_EXPLORER,
        campaign=contract.campaign.id,
        limit=12,
    )
    policy = ResearchPolicyGenome(policy_id="POLICY-DIRECTOR")
    decision = ResearchDirector().decide(
        generation_id="GEN-002",
        packet=packet,
        stagnant_generations=0,
        stagnation_threshold=policy.stagnation_generations,
        default_mix=policy.mutation_operator_mix,
        breakthrough_mix=policy.breakthrough_mutation_mix,
    )
    assert decision.primary_action is ResearchAction.ABLATE
    assert decision.executable_action is ResearchAction.ABLATE
    assert decision.evidence_memory_ids
    assert decision.recommended_mutation_mix != policy.mutation_operator_mix
    assert decision.authority == "SCHEDULING_ONLY"

    with pytest.raises(ValidationError):
        MemoryEpistemics(
            authority="SCIENTIFIC_VERDICT",
            scientific_outcome=ScientificOutcome.VALID_NEGATIVE,
        )
