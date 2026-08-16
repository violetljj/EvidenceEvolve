import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evidence_evolve.benchmarks.algotune_horizon_scaling import (
    ARMS,
    HORIZONS,
    PROTOCOL,
    TASKS,
    _headless_usage_lines,
    _extract_sky,
    _manifest,
)


def test_scaling_protocol_matches_runner_constants() -> None:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    assert tuple(protocol["horizons"]) == HORIZONS
    assert tuple(protocol["arms"]) == ARMS
    assert tuple(item["task"] for item in protocol["tasks"]) == TASKS
    assert protocol["trajectory_design"]["continuous_generations"] == 50
    assert protocol["trajectory_design"]["independent_restart_per_horizon"] is False
    assert protocol["trajectory_design"]["variance_claim_permitted"] is False


def test_headless_usage_prefix_is_cumulative() -> None:
    lines = [
        json.dumps(
            {
                "usage": {
                    "inputTokens": 10,
                    "cacheReadTokens": 20,
                    "outputTokens": 3,
                }
            }
        ),
        json.dumps(
            {
                "usage": {
                    "inputTokens": 4,
                    "cacheReadTokens": 5,
                    "outputTokens": 6,
                }
            }
        ),
    ]
    assert _headless_usage_lines(lines[:1]) == 33
    assert _headless_usage_lines(lines) == 48


def test_protocol_is_frozen_before_run_artifacts(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    assert protocol["authority"] == "FROZEN_BEFORE_FORMAL_SEARCH"
    assert protocol["heldout"]["visibility_during_search"] == "NONE"
    assert not list(tmp_path.rglob("heldout_seeds.json"))


def test_scaling_manifest_is_safe_under_parallel_arm_start(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _index: _manifest(tmp_path, "set_cover"), range(4)))
    manifest = json.loads((tmp_path / "scaling_manifest.json").read_text())
    assert manifest["task"] == "set_cover"
    legacy = json.loads((tmp_path / "manifest.json").read_text())
    assert legacy["task"] == manifest["task"]
    assert legacy["protocol_sha256"] == manifest["protocol_sha256"]


def test_sky_extraction_uses_iteration_not_resettable_lineage_generation(
    tmp_path: Path,
) -> None:
    arm_dir = tmp_path / "evox"
    for horizon in HORIZONS:
        checkpoint = arm_dir / "upstream" / "checkpoints" / f"checkpoint_{horizon}"
        programs = checkpoint / "programs"
        programs.mkdir(parents=True)
        candidate_id = f"candidate-{horizon}"
        (checkpoint / "best_program.py").write_text("class Solver: pass\n")
        (checkpoint / "best_program_info.json").write_text(
            json.dumps(
                {
                    "id": candidate_id,
                    "generation": 0,
                    "saved_at": float(horizon),
                    "metrics": {"raw_speedup": float(horizon)},
                }
            )
        )
        (programs / f"{candidate_id}.json").write_text(
            json.dumps(
                {
                    "id": candidate_id,
                    "generation": 0,
                    "iteration_found": horizon,
                    "metrics": {"correct": True},
                }
            )
        )
    records = _extract_sky(
        arm_dir,
        {"arm": "evox", "tokens": 500, "wall_seconds": 50.0},
        0.0,
    )
    assert [item["selected_generation"] for item in records] == list(HORIZONS)
