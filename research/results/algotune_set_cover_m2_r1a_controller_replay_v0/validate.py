"""Independent checks for the M2-R1A controller replay."""

from __future__ import annotations

import hashlib
import json
import pathlib


HERE = pathlib.Path(__file__).resolve().parent
RESULT = HERE / "result.json"
OUTPUT = HERE / "validation.json"


def main() -> None:
    result = json.loads(RESULT.read_text())
    rows = result["generation_rows"]
    post_gen9 = [row for row in rows if row["generation"] > 9]
    first_repaired = next(
        row["generation"]
        for row in post_gen9
        if row["repaired_mode"] == "BREAKTHROUGH"
    )
    checks = {
        "fifty_generation_rows": len(rows) == 50,
        "post_gen9_escape_is_gen13": first_repaired == 13,
        "recorded_post_gen9_escape_is_gen41": (
            result["recorded_first_breakthrough_after_gen9"] == 41
        ),
        "timing_gain_is_28_generations": result["escape_timing_gain_generations"] == 28,
        "escape_budget_has_no_normal_operator": all(
            row["required_operator_class"] == "STRUCTURAL_ESCAPE_REQUIRED"
            for row in rows
            if row["repaired_mode"] == "BREAKTHROUGH"
        ),
        "r1a_pass": result["r1a_outcome"] == "PASS",
        "zero_calls_and_no_blind": (
            result["new_candidates"] == 0
            and result["model_calls"] == 0
            and result["evaluator_calls"] == 0
            and result["blind_artifacts_read"] is False
        ),
        "prospective_claims_remain_unidentified": len(result["non_identified_claims"])
        == 4,
    }
    failures = sorted(name for name, passed in checks.items() if not passed)
    payload = {
        "schema_version": "1.0",
        "study_id": result["study_id"],
        "status": "READY_TO_SHARE_WITH_CAVEATS" if not failures else "FAILED",
        "checks": checks,
        "failures": failures,
        "result_sha256": hashlib.sha256(RESULT.read_bytes()).hexdigest(),
        "caveat": (
            "R1A identifies controller state transitions only; operator efficacy and "
            "candidate quality require the preregistered prospective R1B experiment."
        ),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if failures:
        raise SystemExit("R1A validation failed: " + ", ".join(failures))


if __name__ == "__main__":
    main()
