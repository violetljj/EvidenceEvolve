import json
from pathlib import Path

import pytest

from evidence_evolve.benchmarks.algotune_horizon_finalize import (
    LOCK_NAME,
    _classify_curve,
    _deduplicate,
    _scientific_outcome,
    lock_checkpoint_portfolio,
)
from evidence_evolve.benchmarks.algotune_horizon_scaling import ARMS, HORIZONS, TASKS
from evidence_evolve.hashing import sha256_file


def _fake_scaling_root(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("class Solver: pass\n", encoding="utf-8")
    digest = sha256_file(candidate)
    for task in TASKS:
        for arm in ARMS:
            arm_dir = tmp_path / task / "arms" / arm
            arm_dir.mkdir(parents=True)
            checkpoints = [
                {
                    "horizon": horizon,
                    "candidate_path": str(candidate),
                    "candidate_sha256": digest,
                    "selected_id": "seed",
                    "selected_generation": 0,
                    "development_raw_speedup": 1.0,
                    "cumulative_tokens": horizon,
                    "cumulative_wall_seconds": float(horizon),
                    "proposal_valid_rate": 1.0,
                }
                for horizon in HORIZONS
            ]
            (arm_dir / "trajectory_result.json").write_text(
                json.dumps({"task": task, "arm": arm, "checkpoints": checkpoints}),
                encoding="utf-8",
            )
    return tmp_path


def test_portfolio_locks_all_checkpoint_slots_before_seeds(tmp_path: Path) -> None:
    root = _fake_scaling_root(tmp_path)
    lock = lock_checkpoint_portfolio(root)
    assert lock["checkpoint_slot_count"] == 100
    assert lock["heldout_existed_at_lock"] is False
    assert (root / LOCK_NAME).exists()


def test_portfolio_refuses_preexisting_heldout_seeds(tmp_path: Path) -> None:
    root = _fake_scaling_root(tmp_path)
    seed_path = root / TASKS[0] / "heldout_seeds.json"
    seed_path.write_text('{"seeds":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="held-out seeds existed"):
        lock_checkpoint_portfolio(root)


def test_deduplicate_maps_identical_slots_to_one_representative() -> None:
    slots = {
        "ada@h003": {"candidate_sha256": "a" * 64, "candidate_path": "/a"},
        "ada@h006": {"candidate_sha256": "a" * 64, "candidate_path": "/a"},
        "evox@h003": {"candidate_sha256": "b" * 64, "candidate_path": "/b"},
    }
    unique, mapping = _deduplicate(slots)
    assert len(unique) == 2
    assert mapping["ada@h003"] == mapping["ada@h006"]


@pytest.mark.parametrize(
    ("heldout", "expected"),
    [
        ({"correct": True, "raw_speedup": 2.0}, "POSITIVE_HEADROOM"),
        ({"correct": True, "raw_speedup": 0.8}, "VALID_NEGATIVE"),
        ({"correct": False, "status_counts": {"INVALID_SOLUTION": 1}}, "INVALID_MECHANICS_OR_ADAPTER"),
        ({"correct": False, "status_counts": {"ERROR_REFERENCE": 1}}, "NOT_EVALUABLE_DATA"),
    ],
)
def test_scientific_outcomes_are_preserved(heldout: dict, expected: str) -> None:
    assert _scientific_outcome(heldout) == expected


def _points(scores: list[float], hashes: list[str] | None = None) -> list[dict]:
    selected_hashes = hashes or [str(index) for index in range(5)]
    return [
        {
            "scientific_outcome": "POSITIVE_HEADROOM",
            "candidate_sha256": selected_hashes[index],
            "heldout": {"raw_speedup": score},
        }
        for index, score in enumerate(scores)
    ]


def test_curve_classification_distinguishes_three_requested_shapes() -> None:
    assert (
        _classify_curve(_points([1, 2, 2, 2, 2], ["a", "b", "b", "b", "b"]))
        == "EARLY_MATURE_BY_H6"
    )
    assert (
        _classify_curve(_points([1, 1, 1.1, 1.25, 1.5]))
        == "SUSTAINED_IMPROVEMENT"
    )
    assert (
        _classify_curve(_points([1, 1, 1, 1, 2]))
        == "PUNCTUATED_BREAKTHROUGH_SIGNAL"
    )
