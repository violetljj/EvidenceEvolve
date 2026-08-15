from __future__ import annotations

import json
from pathlib import Path

from evidence_evolve.benchmarks import algotune_blind
from evidence_evolve.hashing import sha256_file
from tasks.algotune_set_cover.common import generate_problem


def test_set_cover_generator_is_deterministic_and_one_indexed() -> None:
    first = generate_problem(8, 17)
    assert first == generate_problem(8, 17)
    assert first != generate_problem(8, 18)
    assert {item for subset in first for item in subset} == set(range(1, 9))


def test_headless_token_usage_includes_cache_reads(tmp_path: Path) -> None:
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        json.dumps(
            {
                "usage": {
                    "inputTokens": 10,
                    "cacheReadTokens": 20,
                    "outputTokens": 3,
                    "reasoningOutputTokens": 2,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert algotune_blind._headless_token_usage(usage) == 33


def test_finalize_creates_heldout_only_after_candidate_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(algotune_blind, "TEST_COUNT", 3)
    monkeypatch.setattr(algotune_blind, "TEST_REPEATS", 1)
    observed: list[bool] = []

    def fake_evaluate(candidate_path, seeds, *, repeats):
        observed.append((tmp_path / "candidate_lock.json").is_file())
        assert len(seeds) == 3
        return {"correct": True, "raw_speedup": 2.0, "valid_rate": 1.0}

    monkeypatch.setattr(algotune_blind, "evaluate_candidate", fake_evaluate)
    (tmp_path / "protocol_amendment_evox.json").write_text(
        '{"authority":"test"}\n', encoding="utf-8"
    )
    (tmp_path / "adapter_repair_evox.json").write_text(
        '{"authority":"test"}\n', encoding="utf-8"
    )
    for arm in algotune_blind.ARMS:
        arm_dir = tmp_path / "arms" / arm
        arm_dir.mkdir(parents=True)
        candidate = arm_dir / "selected_candidate.py"
        candidate.write_text("class Solver:\n    pass\n", encoding="utf-8")
        (arm_dir / "arm_result.json").write_text(
            json.dumps(
                {
                    "arm": arm,
                    "candidate_path": str(candidate),
                    "candidate_sha256": sha256_file(candidate),
                    "tokens": 1,
                    "wall_seconds": 1.0,
                }
            ),
            encoding="utf-8",
        )

    first = algotune_blind.finalize(tmp_path)
    second = algotune_blind.finalize(tmp_path)

    assert all(observed)
    assert first["candidate_lock_sha256"] == second["candidate_lock_sha256"]
    seed_receipt = json.loads(
        (tmp_path / "heldout_seeds.json").read_text(encoding="utf-8")
    )
    assert seed_receipt["generated_after_candidate_lock"] is True
    assert len(seed_receipt["seeds"]) == 3
    assert set(first["pareto_front"]) == set(algotune_blind.ARMS)
