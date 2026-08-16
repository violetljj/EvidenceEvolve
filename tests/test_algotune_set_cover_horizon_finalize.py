import json
from pathlib import Path

import pytest

from evidence_evolve.benchmarks import algotune_set_cover_horizon_finalize as replay
from evidence_evolve.benchmarks.algotune_horizon_scaling import ARMS, HORIZONS
from evidence_evolve.hashing import sha256_file


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    trajectories = {}
    for arm in ARMS:
        checkpoints = []
        for horizon in HORIZONS:
            candidate = bundle / "candidates" / arm / f"h{horizon:03d}.py"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(
                f"# {arm} h{horizon}\nclass Solver: pass\n", encoding="utf-8"
            )
            checkpoints.append(
                {
                    "horizon": horizon,
                    "candidate_path": str(candidate.relative_to(bundle)),
                    "candidate_sha256": sha256_file(candidate),
                    "selected_id": f"{arm}-{horizon}",
                    "selected_generation": horizon,
                    "development_raw_speedup": float(horizon),
                    "cumulative_tokens": horizon * 10,
                    "cumulative_wall_seconds": float(horizon),
                    "proposal_valid_rate": 1.0,
                }
            )
        trajectories[arm] = {
            "task": "set_cover",
            "arm": arm,
            "checkpoints": checkpoints,
        }
    payload = {
        "protocol_sha256": sha256_file(replay.PROTOCOL),
        "heldout_seeds_generated": False,
        "heldout_evaluation_run": False,
        "horizons": list(HORIZONS),
        "trajectories": trajectories,
    }
    (bundle / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return bundle


def test_lock_bundle_freezes_exactly_twenty_set_cover_slots(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    lock = replay.lock_bundle(bundle, tmp_path / "run")
    assert lock["checkpoint_slot_count"] == 20
    assert lock["heldout_existed_at_lock"] is False
    assert set(lock["tasks"]) == {"set_cover"}


def test_lock_bundle_refuses_preexisting_heldout_seeds(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    run_dir = tmp_path / "run"
    seed_path = run_dir / "set_cover" / replay.SEED_NAME
    seed_path.parent.mkdir(parents=True)
    seed_path.write_text('{"seeds": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="held-out seeds existed"):
        replay.lock_bundle(bundle, run_dir)


def test_bundle_rejects_candidate_hash_drift(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    candidate = bundle / "candidates" / ARMS[0] / f"h{HORIZONS[0]:03d}.py"
    candidate.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exported candidate drift"):
        replay.lock_bundle(bundle, tmp_path / "run")


def test_finalize_requires_passing_canary_before_seeds(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    run_dir = tmp_path / "run"
    replay.lock_bundle(bundle, run_dir)
    with pytest.raises(ValueError, match="passing mechanics canary"):
        replay.finalize(bundle, run_dir)
    assert not (run_dir / "set_cover" / replay.SEED_NAME).exists()


def test_finalize_rejects_canary_for_another_candidate_lock(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    run_dir = tmp_path / "run"
    replay.lock_bundle(bundle, run_dir)
    (run_dir / replay.CANARY_NAME).write_text(
        json.dumps(
            {
                "status": "PASS",
                "checkpoint_candidate_lock_sha256": "wrong",
                "heldout_seeds_generated": False,
                "scientific_evidence": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="valid mechanics canary receipt"):
        replay.finalize(bundle, run_dir)
    assert not (run_dir / "set_cover" / replay.SEED_NAME).exists()
