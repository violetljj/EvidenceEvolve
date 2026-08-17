from __future__ import annotations

import json
import sys
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


def test_m4_v1_replacement_uses_disjoint_sources() -> None:
    replacement = json.loads(
        (
            m4.REPO_ROOT
            / "research/parity/m4_search_value_tournament_v1.protocol.json"
        ).read_text(encoding="utf-8")
    )
    original_tasks = {item["task"] for item in m4._load_protocol()["tasks"]}
    replacement_tasks = {item["task"] for item in replacement["tasks"]}

    assert replacement["replacement_for"]["outcome"] == (
        "INVALID_MECHANICS_OR_ADAPTER"
    )
    assert original_tasks.isdisjoint(replacement_tasks)
    for task in replacement["tasks"]:
        assert m4.sha256_file(m4._source_path(task["task"])) == task["source_sha256"]


def test_budget_admission_caps_ee_cycles_before_formal_v2() -> None:
    admission = json.loads(
        (
            m4.REPO_ROOT / "research/parity/m4_budget_admission_v2.protocol.json"
        ).read_text(encoding="utf-8")
    )
    conditions = admission["common_conditions"]

    assert conditions["observed_token_ceiling"] == 600_000
    assert conditions["proposal_calls"] == 3
    assert conditions["evidence_evolve_cycles"] == 1
    assert conditions["checkpoint_policy"] == "carry_forward_last_valid_candidate"


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


def test_remote_evaluate_cli_does_not_require_run_root(
    monkeypatch, tmp_path: Path
) -> None:
    seeds = tmp_path / "seeds.json"
    seeds.write_text('{"seeds":[1]}', encoding="utf-8")
    output = tmp_path / "output.json"
    candidate = tmp_path / "candidate.py"
    candidate.write_text("pass\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_remote_evaluator(**kwargs):
        observed.update(kwargs)
        return {"correct": True}

    monkeypatch.setattr(m4, "run_remote_evaluator", fake_remote_evaluator)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "search-value-m4",
            "remote-evaluate",
            "--campaign",
            "m4_search_value_tournament_v0",
            "--protocol",
            "research/parity/m4_search_value_tournament_v0.protocol.json",
            "--task",
            "graph_coloring_assign",
            "--candidate",
            str(candidate),
            "--seeds",
            str(seeds),
            "--repeats",
            "1",
            "--workers",
            "2",
            "--output",
            str(output),
        ],
    )

    assert m4.main() == 0
    assert observed["task_name"] == "graph_coloring_assign"


def test_run_search_precreates_shared_manifests_before_parallel_arms(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(m4, "_trial_commands", lambda _run_root: [])

    assert m4.run_search(tmp_path, 4) == []
    manifests = sorted(tmp_path.rglob("m4_manifest.json"))
    assert len(manifests) == 9
