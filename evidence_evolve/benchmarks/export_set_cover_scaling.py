from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

from evidence_evolve.benchmarks.algotune_horizon_scaling import (
    ARMS,
    HORIZONS,
    PROTOCOL,
    REPO_ROOT,
)
from evidence_evolve.hashing import sha256_file


DEFAULT_SOURCE = REPO_ROOT / "runs/algotune_horizon_scaling_v0/set_cover"
DEFAULT_OUTPUT = REPO_ROOT / "research/results/algotune_set_cover_horizon_scaling_v0"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def export_bundle(source: Path, output: Path) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    if (source / "heldout_seeds.json").exists():
        raise ValueError("Set Cover scaling export expects held-out not to have run")
    output.mkdir(parents=True, exist_ok=True)
    trajectories: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for arm in ARMS:
        source_trajectory = source / "arms" / arm / "trajectory_result.json"
        trajectory = json.loads(source_trajectory.read_text(encoding="utf-8"))
        if trajectory.get("task") != "set_cover" or trajectory.get("arm") != arm:
            raise ValueError(f"trajectory identity drift: {arm}")
        if [int(item["horizon"]) for item in trajectory["checkpoints"]] != list(
            HORIZONS
        ):
            raise ValueError(f"trajectory horizon drift: {arm}")
        exported_checkpoints: list[dict[str, Any]] = []
        for item in trajectory["checkpoints"]:
            horizon = int(item["horizon"])
            source_candidate = Path(item["candidate_path"]).resolve()
            if sha256_file(source_candidate) != item["candidate_sha256"]:
                raise ValueError(f"candidate drift: {arm}:h{horizon}")
            relative = Path("candidates") / arm / f"h{horizon:03d}.py"
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_candidate, target)
            digest = sha256_file(target)
            if digest != item["candidate_sha256"]:
                raise ValueError(f"exported candidate drift: {arm}:h{horizon}")
            hashes[relative.as_posix()] = digest
            exported_checkpoints.append(
                {**item, "candidate_path": relative.as_posix()}
            )
        final_result = {
            **trajectory["final_arm_result"],
            "candidate_path": (Path("candidates") / arm / "h050.py").as_posix(),
        }
        if final_result["candidate_sha256"] != exported_checkpoints[-1][
            "candidate_sha256"
        ]:
            raise ValueError(f"final candidate differs from h50 checkpoint: {arm}")
        exported = {
            **trajectory,
            "checkpoints": exported_checkpoints,
            "final_arm_result": final_result,
        }
        trajectories[arm] = exported
        trajectory_path = output / "trajectories" / f"{arm}.json"
        _write_json(trajectory_path, exported)
        hashes[trajectory_path.relative_to(output).as_posix()] = sha256_file(
            trajectory_path
        )

    patterns = {
        "shinka": "EARLY_MATURE_BY_H24",
        "ada": "SUSTAINED_IMPROVEMENT",
        "evox": "LATE_PUNCTUATED_BREAKTHROUGH_SIGNAL",
        "evidence_evolve": "PLATEAU_LOCAL_OPTIMUM",
    }
    rows: list[dict[str, Any]] = []
    for arm in ARMS:
        for checkpoint in trajectories[arm]["checkpoints"]:
            rows.append(
                {
                    "arm": arm,
                    "horizon": checkpoint["horizon"],
                    "development_raw_speedup": checkpoint[
                        "development_raw_speedup"
                    ],
                    "cumulative_tokens": checkpoint["cumulative_tokens"],
                    "cumulative_wall_seconds": checkpoint[
                        "cumulative_wall_seconds"
                    ],
                    "proposal_valid_rate": checkpoint["proposal_valid_rate"],
                    "selected_generation": checkpoint["selected_generation"],
                    "candidate_sha256": checkpoint["candidate_sha256"],
                    "candidate_path": checkpoint["candidate_path"],
                }
            )
    csv_path = output / "curve.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    hashes[csv_path.relative_to(output).as_posix()] = sha256_file(csv_path)

    result = {
        "schema_version": "1.0",
        "campaign": "algotune_set_cover_horizon_scaling_v0",
        "status": "DEVELOPMENT_ONLY_SEARCH_COMPLETE_HELDOUT_NOT_RUN",
        "protocol_sha256": sha256_file(PROTOCOL),
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "temperature": 0.0,
        "horizons": list(HORIZONS),
        "arms": list(ARMS),
        "nested_single_trajectory_per_arm": True,
        "heldout_seeds_generated": False,
        "heldout_evaluation_run": False,
        "checkpoint_slot_count": len(rows),
        "exploratory_development_patterns": patterns,
        "high_variance_claim_permitted": False,
        "cross_engine_superiority_claim_permitted": False,
        "mechanism_claim_permitted": False,
        "trajectories": trajectories,
        "bundle_file_sha256": dict(sorted(hashes.items())),
    }
    result_path = output / "result.json"
    _write_json(result_path, result)

    table_rows = []
    for arm in ARMS:
        checkpoint = trajectories[arm]["checkpoints"][-1]
        table_rows.append(
            f"| {arm} | {checkpoint['development_raw_speedup']:.4f}x | "
            f"{checkpoint['cumulative_tokens']:,} | "
            f"{checkpoint['cumulative_wall_seconds'] / 3600:.2f} h | "
            f"{checkpoint['proposal_valid_rate']:.1%} | {patterns[arm]} |"
        )
    readme = "\n".join(
        [
            "# AlgoTune Set Cover horizon scaling v0",
            "",
            "This bundle freezes the completed development-search trajectories at horizons 3, 6, 12, 24, and 50.",
            "",
            "**Held-out seeds have not been generated and held-out evaluation has not run. These numbers are not a scientific cross-engine ranking.**",
            "",
            "| Arm | h50 dev speedup | tokens | wall | valid rate | exploratory shape |",
            "|---|---:|---:|---:|---:|---|",
            *table_rows,
            "",
            "`curve.csv` contains all 20 checkpoint rows. `trajectories/` preserves full trajectory metadata, and `candidates/` preserves every checkpoint source with hashes recorded in `result.json`.",
            "",
        ]
    )
    (output / "README.md").write_text(readme, encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the frozen Set Cover scaling bundle")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    export_bundle(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
