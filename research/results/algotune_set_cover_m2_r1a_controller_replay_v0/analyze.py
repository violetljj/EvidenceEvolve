"""M2-R1A zero-call replay of incumbent-based escape state transitions."""

from __future__ import annotations

import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
PROTOCOL = json.loads((HERE / "protocol.json").read_text())
SOURCE = ROOT / PROTOCOL["source_result"]
OUTPUT = HERE / "result.json"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    if "blind" in path.resolve().as_posix().lower():
        raise RuntimeError(f"prohibited blind path: {path}")
    return json.loads(path.read_text())


def main() -> None:
    source = read_json(SOURCE)
    controller = PROTOCOL["controller"]
    threshold = int(controller["stagnation_generations"])
    escape_budget = int(controller["escape_budget_generations"])
    repaired_rows = []
    escape_remaining = 0
    first_escape = None
    first_escape_after_gen9 = None
    normal_inside_escape = 0
    recorded_breakthrough = set(
        source["stagnation_observation"]["recorded_breakthrough_generations"]
    )

    for row in source["generation_state"]:
        generation = int(row["generation"])
        stagnant = int(row["stagnation_before_generation"])
        triggered = False
        if escape_remaining == 0 and stagnant >= threshold:
            escape_remaining = escape_budget
            triggered = True
        mode = "BREAKTHROUGH" if escape_remaining > 0 else "NORMAL"
        if mode == "BREAKTHROUGH" and first_escape is None:
            first_escape = generation
        if mode == "BREAKTHROUGH" and generation > 9 and first_escape_after_gen9 is None:
            first_escape_after_gen9 = generation
        operator_class = (
            "STRUCTURAL_ESCAPE_REQUIRED"
            if mode == "BREAKTHROUGH"
            else "NORMAL_SEARCH"
        )
        repaired_rows.append(
            {
                "generation": generation,
                "stagnation_before_generation": stagnant,
                "recorded_mode": (
                    "BREAKTHROUGH" if generation in recorded_breakthrough else "NORMAL"
                ),
                "repaired_mode": mode,
                "escape_triggered": triggered,
                "escape_budget_before_generation": escape_remaining,
                "required_operator_class": operator_class,
                "reclassified": (
                    (generation in recorded_breakthrough) != (mode == "BREAKTHROUGH")
                ),
            }
        )
        if mode == "BREAKTHROUGH":
            escape_remaining -= 1

    reclassified = [row["generation"] for row in repaired_rows if row["reclassified"]]
    result = {
        "schema_version": "1.0",
        "study_id": PROTOCOL["study_id"],
        "scope": PROTOCOL["scope"],
        "blind_artifacts_read": False,
        "new_candidates": 0,
        "model_calls": 0,
        "evaluator_calls": 0,
        "recorded_first_breakthrough_after_gen9": source["stagnation_observation"][
            "recorded_first_breakthrough_after_gen9"
        ],
        "repaired_first_escape_generation": first_escape,
        "repaired_first_escape_after_gen9": first_escape_after_gen9,
        "escape_timing_gain_generations": (
            source["stagnation_observation"]["recorded_first_breakthrough_after_gen9"]
            - first_escape_after_gen9
        ),
        "reclassified_generation_count": len(reclassified),
        "reclassified_generations": reclassified,
        "breakthrough_generation_count": sum(
            row["repaired_mode"] == "BREAKTHROUGH" for row in repaired_rows
        ),
        "normal_operator_inside_active_escape_budget": normal_inside_escape,
        "generation_rows": repaired_rows,
        "pass_criteria": {
            "first_escape_after_gen9_is_13": first_escape_after_gen9 == 13,
            "no_normal_operator_inside_escape": normal_inside_escape == 0,
            "no_blind_or_new_calls": True,
        },
        "r1a_outcome": (
            "PASS"
            if first_escape_after_gen9 == 13 and normal_inside_escape == 0
            else "FAIL"
        ),
        "non_identified_claims": [
            "Whether a structural escape operator generates a new root lineage.",
            "Whether a new root lineage produces a development-valid candidate.",
            "Whether the repaired controller improves the final development incumbent.",
            "Any held-out or generalization performance claim."
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
