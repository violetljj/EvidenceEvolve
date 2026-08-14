import json
from datetime import datetime, timezone

import pytest

from evidence_evolve.artifacts import (
    ReceiptIntegrityError,
    environment_receipt,
    load_receipt,
    write_receipt,
)
from evidence_evolve.budgets import BudgetExceeded, BudgetLedger
from evidence_evolve.governance.gate_engine import GateEngine
from evidence_evolve.models import (
    Budgets,
    EvaluationInput,
    EvaluationReceipt,
    MechanicsStatus,
    ResearchStage,
    ScientificOutcome,
)


def test_budget_reservation_is_idempotent(tmp_path) -> None:
    ledger = BudgetLedger(
        tmp_path / "research.db", Budgets(proposal_calls=1)
    )
    assert ledger.reserve("proposal_calls", 1, "proposal:C1")
    assert not ledger.reserve("proposal_calls", 1, "proposal:C1")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("proposal_calls", 1, "proposal:C2")


def test_receipt_hash_detects_tampering(tmp_path, contract, candidate) -> None:
    evaluation = EvaluationInput(
        contract_sha256=contract.lock.content_sha256,
        candidate=candidate,
        stage=ResearchStage.H0_REAL_HEADROOM,
        mechanics_status=MechanicsStatus.PASS,
        data_eligible=True,
        metrics={"false_block_delta_pp": 0.0},
        controls={"wrong_factor": True, "zero_factor": True},
        scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
    )
    receipt = EvaluationReceipt(
        receipt_id="R1",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        campaign_id=contract.campaign.id,
        candidate_id=candidate.candidate_id,
        base_commit=contract.campaign.base_commit,
        evaluator_hashes={"evaluator": "c" * 64},
        data_hashes={"truth": "b" * 64},
        seed=0,
        command=["python", "evaluate.py"],
        elapsed_seconds=0.1,
        environment=environment_receipt(),
        evaluation_input=evaluation,
        verdict=GateEngine(contract).evaluate(evaluation),
    )
    path = tmp_path / "receipt.json"
    write_receipt(path, receipt)
    assert load_receipt(path).receipt.candidate_id == candidate.candidate_id
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["receipt"]["seed"] = 7
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReceiptIntegrityError):
        load_receipt(path)

