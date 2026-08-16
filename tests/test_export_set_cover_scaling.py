import json
from pathlib import Path

from evidence_evolve.benchmarks.algotune_horizon_scaling import ARMS, HORIZONS
from evidence_evolve.benchmarks.export_set_cover_scaling import export_bundle
from evidence_evolve.hashing import sha256_file


def test_export_bundle_is_self_contained_and_preserves_hashes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    for arm in ARMS:
        arm_dir = source / "arms" / arm
        arm_dir.mkdir(parents=True)
        checkpoints = []
        for horizon in HORIZONS:
            candidate = arm_dir / f"h{horizon:03d}.py"
            candidate.write_text(
                f"# {arm} h{horizon}\nclass Solver: pass\n", encoding="utf-8"
            )
            checkpoints.append(
                {
                    "horizon": horizon,
                    "candidate_path": str(candidate),
                    "candidate_sha256": sha256_file(candidate),
                    "selected_id": f"{arm}-{horizon}",
                    "selected_generation": horizon,
                    "development_raw_speedup": float(horizon),
                    "cumulative_tokens": horizon * 10,
                    "cumulative_wall_seconds": float(horizon * 2),
                    "proposal_valid_rate": 1.0,
                }
            )
        final = {
            "arm": arm,
            "candidate_path": checkpoints[-1]["candidate_path"],
            "candidate_sha256": checkpoints[-1]["candidate_sha256"],
            "tokens": 500,
            "wall_seconds": 100.0,
        }
        (arm_dir / "trajectory_result.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "task": "set_cover",
                    "arm": arm,
                    "checkpoints": checkpoints,
                    "final_arm_result": final,
                }
            ),
            encoding="utf-8",
        )
    result = export_bundle(source, output)
    assert result["checkpoint_slot_count"] == 20
    assert result["heldout_evaluation_run"] is False
    for arm in ARMS:
        trajectory = result["trajectories"][arm]
        assert trajectory["final_arm_result"]["candidate_path"] == (
            f"candidates/{arm}/h050.py"
        )
        for checkpoint in trajectory["checkpoints"]:
            assert sha256_file(output / checkpoint["candidate_path"]) == checkpoint[
                "candidate_sha256"
            ]
