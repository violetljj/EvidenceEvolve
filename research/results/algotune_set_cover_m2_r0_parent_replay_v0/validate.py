"""Independent validation for the M2-R0 development-only replay."""

from __future__ import annotations

import collections
import hashlib
import json
import math
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
RESULT = HERE / "result.json"
M1 = ROOT / "research/results/algotune_set_cover_mechanism_autopsy_v0/result.json"
CAMPAIGN = ROOT / "runs/algotune_horizon_scaling_v0/set_cover/arms/evidence_evolve/campaign"
OUTPUT = HERE / "validation.json"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    if "blind" in path.resolve().as_posix().lower():
        raise RuntimeError(f"prohibited blind path: {path}")
    return json.loads(path.read_text())


def close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)


def main() -> None:
    result = read_json(RESULT)
    m1 = read_json(M1)["arm_observability"]["evidence_evolve"]
    valid_rows = [row for row in m1["generation_rows"] if row["valid"]]

    traces = []
    for index in range(1, 51):
        traces.append(
            read_json(
                CAMPAIGN
                / "generations"
                / f"GEN-{index:03d}"
                / "policy_effect_trace.json"
            )
        )

    exposure: collections.Counter[str] = collections.Counter()
    candidate_slots = 0
    for trace in traces:
        for candidate_id in trace["parent_pools_by_island"].get("main", []):
            if candidate_id != "SEED":
                exposure[candidate_id] += 1
                candidate_slots += 1

    observed = next(
        row for row in result["policy_summaries"] if row["policy_id"] == "observed"
    )
    guardrail = next(
        row
        for row in result["policy_summaries"]
        if row["policy_id"] == "incumbent_guardrail"
    )
    escape = next(
        row
        for row in result["policy_summaries"]
        if row["policy_id"] == "guardrail_stagnation_escape"
    )

    shares = [count / candidate_slots for count in exposure.values()]
    independently_computed_hhi = sum(share * share for share in shares)
    checks = {
        "development_only_flags": (
            result["blind_artifacts_read"] is False
            and result["new_candidates"] == 0
            and result["model_calls"] == 0
            and result["evaluator_calls"] == 0
        ),
        "fifty_frozen_traces": len(traces) == 50,
        "thirty_six_valid_candidates": (
            len(valid_rows) == 36
            and result["candidate_population"]["development_valid_candidates"] == 36
        ),
        "best_candidate_gen9": (
            max(valid_rows, key=lambda row: float(row["raw_speedup"]))["candidate_id"]
            == "GEN-009-C01"
            == result["candidate_population"]["best_frozen_candidate"]
        ),
        "observed_parent_exposure_exact": (
            candidate_slots == 85
            and dict(exposure.most_common()) == observed["parent_exposure"]
            and exposure["GEN-013-C01"] == observed["gen13_exposure"] == 37
            and exposure["GEN-009-C01"] == observed["gen9_exposure"] == 16
        ),
        "observed_concentration_formula": (
            close(observed["parent_hhi"], independently_computed_hhi)
            and close(observed["max_parent_share"], max(shares))
            and close(observed["effective_parent_count"], 1 / independently_computed_hhi)
        ),
        "guardrail_removes_gen13_pressure": (
            guardrail["gen13_exposure"] == 0
            and guardrail["sub_guardrail_slots"] == 0
            and guardrail["mean_parent_quality_ratio"]
            > observed["mean_parent_quality_ratio"]
        ),
        "frozen_pool_has_one_root_lineage": (
            result["candidate_population"]["unique_root_lineages"] == 1
            and result["candidate_population"]["root_lineage_ids"] == ["GEN-007-C01"]
            and observed["distinct_root_lineages"] == 1
            and guardrail["distinct_root_lineages"] == 1
        ),
        "escape_is_required_at_gen13": (
            escape["first_escape_generation"] == 13
            and escape["escape_required_slots"] == 38
        ),
        "recorded_breakthrough_is_delayed": (
            result["stagnation_observation"]["recorded_breakthrough_generations"]
            == [3, 4, 5, 6, 7, 41, 42, 43]
            and result["stagnation_observation"]["recorded_first_breakthrough_after_gen9"]
            == 41
        ),
        "sensitivity_preserves_gen13_nonselection": all(
            row["gen13_exposure"] == 0
            for row in result["near_incumbent_sensitivity"]
        ),
        "static_replay_keeps_best_frozen_candidate": all(
            claim in result["non_identified_claims"]
            for claim in [
                "Counterfactual candidates that would have been generated from different parents.",
                "Counterfactual final development or held-out quality under a changed parent policy.",
            ]
        ),
    }
    failures = sorted(name for name, passed in checks.items() if not passed)
    validation = {
        "schema_version": "1.0",
        "study_id": result["study_id"],
        "status": "READY_TO_SHARE_WITH_CAVEATS" if not failures else "FAILED",
        "checks": checks,
        "failures": failures,
        "result_sha256": hashlib.sha256(RESULT.read_bytes()).hexdigest(),
        "review_caveat": (
            "The replay identifies parent-pressure changes only over the frozen "
            "development candidate pool; it does not identify counterfactual generated "
            "candidates or final development/held-out quality."
        ),
    }
    OUTPUT.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n")
    if failures:
        raise SystemExit("validation failed: " + ", ".join(failures))


if __name__ == "__main__":
    main()
