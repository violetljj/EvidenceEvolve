from __future__ import annotations

import json

from evidence_evolve.benchmarks import engine_selection_r2 as selection
from evidence_evolve.benchmarks import engine_selection_r2_runner as runner


def _blocks(arms: list[str], repeats: tuple[int, ...], scores: dict[str, list[float]]) -> list[dict[str, object]]:
    tasks = [item["task"] for item in selection.load_protocol()["tasks"]]
    blocks: list[dict[str, object]] = []
    for arm in arms:
        for task_index, task in enumerate(tasks):
            for repeat in repeats:
                improvement = scores[arm][task_index]
                blocks.append({
                    "task": task,
                    "repeat": repeat,
                    "arm": arm,
                    "run_valid": True,
                    "observed_tokens": 10_000_000 if arm == "evox" else 100_000,
                    "wall_seconds": 100.0,
                    "heldout": {"correct": True, "raw_speedup": 1.0 + improvement},
                })
    return blocks


def test_protocol_makes_tokens_account_only() -> None:
    protocol = selection.load_protocol()
    conditions = protocol["common_conditions"]
    assert conditions["token_call_launch_ceiling"] is None
    assert conditions["token_hard_ceiling"] is None
    assert protocol["ranking"]["lexicographic_order"][-1] == "lower_total_tokens"
    assert runner.NO_TOKEN_STOP > 10**18
    assert runner.RUNNER_MODULE != "__main__"
    assert protocol["common_conditions"]["provider_version"].startswith("codex-cli ")
    assert protocol["common_conditions"]["remote_evaluation_slots"] == 2
    assert protocol["common_conditions"]["evaluator_workers_per_active_run"] == 12


def test_round_one_advances_higher_quality_despite_far_more_tokens() -> None:
    scores = {
        "vanilla": [0.20, 0.20, 0.20],
        "ada": [0.30, 0.30, 0.30],
        "shinka": [0.10, 0.10, 0.10],
        "evox": [0.42, 0.42, 0.42],
    }
    result = selection.score_round_1(_blocks(list(scores), (1,), scores))
    assert result["ranking"][0] == "evox"
    assert result["finalists"] == ["evox", "ada"]
    assert result["tokens_are_decision_primary"] is False


def test_quality_equivalence_uses_robustness_before_tokens() -> None:
    arms = ["ada", "evox"]
    scores = {
        "ada": [0.40, 0.40, 0.40],
        "evox": [0.60, 0.30, 0.30],
    }
    round_one = {
        "protocol_sha256": selection.sha256_file(selection.PROTOCOL),
        "finalists": arms,
    }
    result = selection.score_final(round_one, _blocks(arms, (2, 3), scores))
    assert result["BEST_QUALITY_ENGINE"] == "ada"
    assert result["MOST_ROBUST_ENGINE"] == "ada"


def test_invalid_heldout_cannot_win_on_reported_speedup() -> None:
    scores = {
        "vanilla": [0.20, 0.20, 0.20],
        "ada": [0.30, 0.30, 0.30],
        "shinka": [0.10, 0.10, 0.10],
        "evox": [0.90, 0.90, 0.90],
    }
    blocks = _blocks(list(scores), (1,), scores)
    for block in blocks:
        if block["arm"] == "evox":
            block["heldout"]["correct"] = False  # type: ignore[index]
    result = selection.score_round_1(blocks)
    assert result["ranking"][0] == "ada"


def test_concurrent_identical_manifest_is_accepted(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "task"
    run_dir.mkdir()
    monkeypatch.setattr(runner, "_conditions", lambda _task: {"token_policy": "account"})
    runner._manifest(run_dir, "pde_heat1d", 1, "ROUND_1")
    runner._manifest(run_dir, "pde_heat1d", 1, "ROUND_1")


def test_stage_stops_after_first_failed_paired_block(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, int, str]] = []

    def fake_item(_root, task, repeat, arm):
        calls.append((task, repeat, arm))
        return {
            "task": task,
            "repeat": repeat,
            "arm": arm,
            "state": "FAILED" if task == "first" and arm == "ada" else "SUCCEEDED",
        }

    monkeypatch.setattr(runner, "_run_item", fake_item)
    items = [
        (task, 1, arm)
        for task in ("first", "second")
        for arm in ("vanilla", "ada", "shinka", "evox")
    ]
    result = runner._run_parallel(tmp_path, items, 4, "summary.json")

    assert len(result) == 4
    assert {item[0] for item in calls} == {"first"}


def test_every_development_evaluation_is_append_logged(tmp_path, monkeypatch) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("class Solver: pass\n")
    monkeypatch.setenv("EE_ENGINE_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("EE_ENGINE_ARM", "ada")
    monkeypatch.setenv("EE_ENGINE_REPEAT", "1")
    monkeypatch.setenv("EE_ENGINE_ARM_STARTED_MONOTONIC", "0")
    result = {
        "metrics": {"raw_speedup": 1.25},
        "controls": {"candidate_valid": True},
        "remote_receipt_sha256": "a" * 64,
    }

    runner._record_development_observation(candidate, "pde_heat1d", result)
    result["metrics"]["raw_speedup"] = 1.20
    runner._record_development_observation(candidate, "pde_heat1d", result)

    ledger = (
        tmp_path
        / "runs/pde_heat1d/repeat_01/arms/ada/development_observations.jsonl"
    )
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [item["evaluation_index"] for item in rows] == [1, 2]
    assert [item["incumbent_refreshed"] for item in rows] == [True, False]
