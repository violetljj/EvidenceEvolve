from __future__ import annotations

import json
from pathlib import Path

from evidence_evolve.benchmarks import engine_selection_r1 as selection
from evidence_evolve.benchmarks import engine_selection_r1_runner as runner
from tasks.algotune_set_cover import codex_headless


def _block(
    task: str,
    repeat: int,
    arm: str,
    score_50: float,
    score_100: float,
    score_200: float,
    *,
    valid: bool = True,
) -> dict[str, object]:
    return {
        "task": task,
        "repeat": repeat,
        "arm": arm,
        "run_valid": valid,
        "observed_tokens": 190_000,
        "wall_seconds": 100.0,
        "proposal_valid_rate": 1.0,
        "checkpoints": [
            {
                "token_budget": budget,
                "candidate_cumulative_tokens": min(budget, 190_000),
                "heldout": {"correct": True, "raw_speedup": 1.0 + score},
            }
            for budget, score in (
                (50_000, score_50),
                (100_000, score_100),
                (200_000, score_200),
            )
        ],
    }


def _core_blocks(scores: dict[str, list[float]]) -> list[dict[str, object]]:
    protocol = selection.load_protocol()
    tasks = [task["task"] for task in protocol["tasks"] if task["role"] == "core"]
    blocks: list[dict[str, object]] = []
    for task_index, task in enumerate(tasks):
        for repeat in (1, 2):
            for arm in protocol["arms"]:
                score = scores[arm][task_index]
                blocks.append(_block(task, repeat, arm, score, score, score))
    return blocks


def test_protocol_freezes_four_new_exact_upstream_tasks() -> None:
    protocol = selection.load_protocol()

    assert [task["role"] for task in protocol["tasks"]] == [
        "core",
        "core",
        "core",
        "reserve",
    ]
    assert {task["task"] for task in protocol["tasks"]}.isdisjoint(
        selection.consumed_m4_tasks()
    )
    assert protocol["common_conditions"]["token_checkpoints"] == [
        50_000,
        100_000,
        200_000,
    ]
    assert protocol["scoring"]["tie_epsilon"] == 0.0


def test_unique_non_vanilla_three_task_win_selects_directly() -> None:
    result = selection.score_core(
        _core_blocks(
            {
                "vanilla": [0.2, 0.2, 0.2],
                "ada": [0.5, 0.5, 0.5],
                "shinka": [0.1, 0.1, 0.1],
                "evox": [0.3, 0.3, 0.3],
            }
        )
    )

    assert result["reserve_required"] is False
    assert result["DEEP_DEFAULT_200K"] == "ada"
    assert result["GLOBAL_DEFAULT"] == "ada"
    assert result["budget_results"]["200000"]["pairwise_matrix"]["ada"][
        "opponents"
    ]["vanilla"]["wins"] == 3


def test_two_of_three_non_vanilla_boundary_opens_reserve() -> None:
    result = selection.score_core(
        _core_blocks(
            {
                "vanilla": [0.2, 0.2, 0.6],
                "ada": [0.5, 0.5, 0.5],
                "shinka": [0.1, 0.1, 0.1],
                "evox": [0.3, 0.3, 0.3],
            }
        )
    )

    assert result["reserve_required"] is True
    assert "NON_VANILLA_REPLACEMENT_BOUNDARY_2_OF_3" in result["reserve_reasons"]
    assert result["GLOBAL_DEFAULT"] == "PENDING_RESERVE"


def test_invalid_non_vanilla_leader_falls_back_to_vanilla() -> None:
    blocks = _core_blocks(
        {
            "vanilla": [0.2, 0.2, 0.2],
            "ada": [1.0, 1.0, 1.0],
            "shinka": [0.1, 0.1, 0.1],
            "evox": [0.3, 0.3, 0.3],
        }
    )
    next(block for block in blocks if block["arm"] == "ada")["run_valid"] = False

    result = selection.score_core(blocks)

    assert result["GLOBAL_DEFAULT"] == "vanilla"
    assert "TENTATIVE_LEADER_NOT_6_OF_6_VALID" in result["fallback_reasons"]


def test_100k_200k_leader_conflict_opens_reserve() -> None:
    protocol = selection.load_protocol()
    tasks = [task["task"] for task in protocol["tasks"] if task["role"] == "core"]
    blocks = []
    for task in tasks:
        for repeat in (1, 2):
            for arm in protocol["arms"]:
                score_100 = 0.6 if arm == "vanilla" else 0.4 if arm == "evox" else 0.1
                score_200 = 0.7 if arm == "evox" else 0.3 if arm == "vanilla" else 0.1
                blocks.append(_block(task, repeat, arm, score_100, score_100, score_200))

    result = selection.score_core(blocks)

    assert result["CHEAP_DEFAULT_100K"] == "vanilla"
    assert result["DEEP_DEFAULT_200K"] == "evox"
    assert "100K_AND_200K_LEADERS_CONFLICT" in result["reserve_reasons"]


def test_token_checkpoints_never_select_an_over_budget_candidate(tmp_path: Path) -> None:
    candidates = [
        {"id": "seed", "code": "seed", "score": 1.0, "tokens": 0},
        {"id": "a", "code": "a", "score": 2.0, "tokens": 60_000},
        {"id": "b", "code": "b", "score": 3.0, "tokens": 210_000},
    ]

    checkpoints = runner._select_checkpoints(
        tmp_path, candidates, [{"tokens": 60_000, "valid": True}]
    )

    assert [item["candidate_id"] for item in checkpoints] == ["seed", "a", "a"]
    assert all(
        item["candidate_cumulative_tokens"] <= item["token_budget"]
        for item in checkpoints
    )


def test_headless_launch_gate_counts_all_accounted_token_classes(
    monkeypatch, tmp_path: Path
) -> None:
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        json.dumps(
            {
                "usage": {
                    "inputTokens": 10,
                    "cacheReadTokens": 20,
                    "outputTokens": 30,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EE_HEADLESS_USAGE_LOG", str(usage))

    assert codex_headless._observed_usage() == 60


def test_search_precreates_shared_task_workspaces_before_parallel_arms(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "_core_items", lambda _root: [])
    (tmp_path / "mechanics_smoke_receipt.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "protocol_sha256": selection.sha256_file(selection.PROTOCOL),
                "scientific_authority": False,
            }
        ),
        encoding="utf-8",
    )

    assert runner.search_core(tmp_path, 1) == []
    manifests = list(tmp_path.rglob("engine_selection_manifest.json"))
    evaluators = list(tmp_path.rglob("task/evaluator.py"))

    assert len(manifests) == 6
    assert len(evaluators) == 6
    assert all(
        "engine_selection_r1_runner import remote_development_evaluate"
        in path.read_text(encoding="utf-8")
        for path in evaluators
    )


def test_formal_search_rejects_missing_smoke_receipt(tmp_path: Path) -> None:
    try:
        runner.search_core(tmp_path, 1)
    except ValueError as exc:
        assert "mechanics smoke" in str(exc)
    else:
        raise AssertionError("formal search ran without mechanics admission")


def test_reserve_promotes_only_after_three_of_four_wins_vs_vanilla() -> None:
    core = selection.score_core(
        _core_blocks(
            {
                "vanilla": [0.2, 0.2, 0.6],
                "ada": [0.5, 0.5, 0.5],
                "shinka": [0.1, 0.1, 0.1],
                "evox": [0.3, 0.3, 0.3],
            }
        )
    )
    reserve_task = next(
        task["task"] for task in selection.load_protocol()["tasks"]
        if task["role"] == "reserve"
    )
    assert set(core["reserve_participants"]) == {"ada", "vanilla"}
    blocks = [
        _block(
            reserve_task,
            repeat,
            arm,
            0.7 if arm == "ada" else 0.2,
            0.7 if arm == "ada" else 0.2,
            0.7 if arm == "ada" else 0.2,
        )
        for repeat in (1, 2)
        for arm in core["reserve_participants"]
    ]

    result = selection.score_reserve(core, blocks)

    assert result["GLOBAL_DEFAULT"] == "ada"
    assert result["promotion_reason"] == (
        "NON_VANILLA_WON_AT_LEAST_3_OF_4_VS_VANILLA"
    )
