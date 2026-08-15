from __future__ import annotations

import json
from pathlib import Path

from tasks.scalefree_risk_fusion_v0.locked_eval import (
    DEVELOPMENT_SEED,
    score_without_record,
)


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "scalefree_risk_fusion_v0"


def test_baseline_is_valid_and_deterministic() -> None:
    first = score_without_record(str(TASK / "candidate.py"), seed=DEVELOPMENT_SEED)
    second = score_without_record(str(TASK / "candidate.py"), seed=DEVELOPMENT_SEED)
    assert first["valid"] is True
    assert first["combined_score"] == second["combined_score"]
    assert 0.0 <= float(first["approach_macro_f1"]) <= 1.0
    assert 0.0 <= float(first["false_clear"]) <= 1.0
    assert 0.0 <= float(first["false_block"]) <= 1.0


def test_forbidden_import_is_rejected(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "import os\ndef compute_risk(features):\n    return 0.5\n", encoding="utf-8"
    )
    result = score_without_record(str(candidate), seed=DEVELOPMENT_SEED)
    assert result["valid"] is False
    assert result["combined_score"] == 0.0


def test_evaluation_ledger_retains_candidate(monkeypatch, tmp_path: Path) -> None:
    from tasks.scalefree_risk_fusion_v0.locked_eval import evaluate_and_record

    ledger = tmp_path / "evaluations.jsonl"
    monkeypatch.setenv("EE_SFR_EVAL_LEDGER", str(ledger))
    result = evaluate_and_record(str(TASK / "candidate.py"))
    record = json.loads(ledger.read_text(encoding="utf-8"))
    assert result["valid"] is True
    assert record["evaluation_index"] == 0
    assert Path(record["candidate_path"]).is_file()


def test_final_reevaluation_is_not_an_iteration(monkeypatch, tmp_path: Path) -> None:
    from tasks.scalefree_risk_fusion_v0.locked_eval import evaluate_and_record

    ledger = tmp_path / "evaluations.jsonl"
    monkeypatch.setenv("EE_SFR_EVAL_LEDGER", str(ledger))
    monkeypatch.setenv("EE_SFR_EXPECTED_ITERATIONS", "1")
    evaluate_and_record(str(TASK / "candidate.py"))
    evaluate_and_record(str(TASK / "candidate.py"))
    evaluate_and_record(str(TASK / "candidate.py"))
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [record["role"] for record in records] == [
        "initial_baseline",
        "solution_candidate",
        "final_best_reevaluation",
    ]
    assert [record["evox_iteration"] for record in records] == [None, 1, None]
