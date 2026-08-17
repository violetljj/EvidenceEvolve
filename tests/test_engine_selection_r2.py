from __future__ import annotations

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
