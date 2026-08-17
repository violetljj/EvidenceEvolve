from __future__ import annotations

import json
from pathlib import Path

from evidence_evolve.benchmarks import search_value_m4 as m4


def _block(task: str, repeat: int, arm: str, score: float) -> dict[str, object]:
    return {
        "task": task,
        "repeat": repeat,
        "arm": arm,
        "final_valid": True,
        "valid_final_heldout_improvement": score,
        "heldout_anytime_auc": score,
        "success": score > 0.0,
        "wall_seconds": 100.0,
        "observed_tokens": 1000,
    }


def test_m4_protocol_binds_three_fresh_heterogeneous_sources() -> None:
    protocol = m4._load_protocol()

    assert [item["task"] for item in protocol["tasks"]] == [
        "graph_coloring_assign",
        "convolve_1d",
        "ode_lorenz96_nonchaotic",
    ]
    assert protocol["ee_policy_checkpoint"].startswith("fd53fba")
    assert protocol["common_conditions"]["evaluator_workers_per_active_run"] == 4
    assert protocol["superiority_claim_permitted"] is False


def test_m4_continue_gate_requires_two_task_wins_and_external_nonloss(
    tmp_path: Path,
) -> None:
    (tmp_path / "portfolio_candidate_lock.json").write_text("{}", encoding="utf-8")
    tasks = [item["task"] for item in m4._load_protocol()["tasks"]]
    blocks = []
    for task_index, task in enumerate(tasks):
        for repeat in (1, 2, 3):
            for arm in m4.ARMS:
                score = 0.1
                if arm == "evidence_evolve":
                    score = 0.3 if task_index < 2 else 0.05
                elif arm == "vanilla":
                    score = 0.2
                elif arm in {"shinka", "ada"}:
                    score = 0.25
                blocks.append(_block(task, repeat, arm, score))

    result = m4._aggregate(tmp_path, blocks)

    assert result["decision"] == "CONTINUE_EE_SEARCH_RESEARCH"
    assert result["continue_gate_met"] is True


def test_m4_external_dominance_stops_search_core(tmp_path: Path) -> None:
    (tmp_path / "portfolio_candidate_lock.json").write_text("{}", encoding="utf-8")
    blocks = []
    for task in [item["task"] for item in m4._load_protocol()["tasks"]]:
        for repeat in (1, 2, 3):
            for arm in m4.ARMS:
                score = 0.1 if arm == "evidence_evolve" else 0.2
                blocks.append(_block(task, repeat, arm, score))

    result = m4._aggregate(tmp_path, blocks)

    assert result["decision"] == "STOP_EE_SEARCH_CORE"
    assert result["stop_gate_met"] is True
