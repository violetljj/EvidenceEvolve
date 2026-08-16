from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evidence_evolve.hashing import sha256_file


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RUN = ROOT / "runs/algotune_set_cover_m2_r1b_dev_v1"
ARMS = ("controller_only", "radical_roots")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    result_path = HERE / "result.json"
    result = _read(result_path)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    check(
        "top_level_development_only",
        result["scope"] == "PROSPECTIVE_SET_COVER_DEVELOPMENT_ONLY_MECHANISM_TEST"
        and result["blind_artifacts_read"] is False
        and result["blind_evaluator_calls"] == 0
        and result["confirmation_runs"] == 0,
        result["scope"],
    )
    for arm in ARMS:
        campaign = RUN / arm / "campaign"
        arm_result = _read(RUN / arm / "arm_result.json")
        manifest = _read(campaign / "m2_controller_manifest.json")
        states = []
        forced_valid = 0
        forced_total = 0
        dev_receipts = 0
        source_sets: set[tuple[str, ...]] = set()
        guardrail_violations: list[str] = []
        for generation in range(1, 17):
            generation_id = f"GEN-{generation:03d}"
            candidate_id = f"{generation_id}-C01"
            generation_dir = campaign / "generations" / generation_id
            state = _read(generation_dir / "m2_controller_state.json")
            states.append(state)
            incumbent = float(state["incumbent_value_before"])
            tolerance = max(abs(incumbent), 1e-12) * 0.005
            for parent in state["preferred_parent_ids"]:
                value = state["objective_values"].get(parent)
                if value is None or float(value) < incumbent - tolerance:
                    guardrail_violations.append(f"{generation_id}:{parent}")
            intentional_seed_restart = bool(
                arm == "radical_roots"
                and state["mode"] == "BREAKTHROUGH"
                and state["mutation_assignment"] == "restart"
            )
            if (
                state["parent_pool"] == ["SEED"]
                and state["objective_values"]
                and not intentional_seed_restart
            ):
                guardrail_violations.append(
                    f"{generation_id}:unexpected_seed_fallback"
                )
            proposal_path = generation_dir / "proposals" / f"{candidate_id}.json"
            proposal = _read(proposal_path) if proposal_path.is_file() else None
            forced = bool(
                proposal
                and state["mode"] == "BREAKTHROUGH"
                and state["mutation_assignment"] == "restart"
                and proposal["acquisition"]["candidate"]["genetic_parent_id"] == "SEED"
            )
            forced_total += int(forced)
            receipt_dir = campaign / "candidates" / candidate_id / "receipts"
            receipt_paths = sorted(
                path
                for path in receipt_dir.glob("*.json")
                if not path.name.endswith(".mechanism.json")
            )
            if not receipt_paths:
                continue
            receipt = _read(receipt_paths[0])["receipt"]
            evaluation = receipt["evaluation_input"]
            dev_receipts += 1
            source_sets.add(tuple(sorted(receipt["data_hashes"])))
            valid = bool(
                evaluation["mechanics_status"] == "PASS"
                and evaluation["data_eligible"]
                and not evaluation["protocol_violations"]
                and evaluation["controls"].get("candidate_valid")
                and evaluation["controls"].get("development_only")
            )
            forced_valid += int(forced and valid)
        check(
            f"{arm}_fixed_horizon",
            len(states) == 16,
            len(states),
        )
        check(
            f"{arm}_strict_clock",
            all(
                state["stagnant_generations_before"] < 3
                or state["mode"] == "BREAKTHROUGH"
                for state in states
            ),
            [
                state["generation_id"]
                for state in states
                if state["stagnant_generations_before"] >= 3
                and state["mode"] != "BREAKTHROUGH"
            ],
        )
        check(
            f"{arm}_archive_parent_guardrail",
            not guardrail_violations,
            guardrail_violations,
        )
        check(
            f"{arm}_controller_scope",
            manifest["blind_artifacts_read"] is False
            and arm_result["metadata"]["evidence_scope"] == "DEVELOPMENT_ONLY"
            and arm_result["metadata"]["blind_evaluator_calls"] == 0,
            arm_result["metadata"]["evidence_scope"],
        )
        check(
            f"{arm}_only_development_receipts",
            dev_receipts > 0
            and source_sets == {("algotune-set-cover-dev-b-v0",)},
            {"receipts": dev_receipts, "source_sets": sorted(source_sets)},
        )
        observed = result["arms"][arm]
        check(
            f"{arm}_root_counts_recomputed",
            observed["new_structural_root_lineages"] == forced_total
            and observed["dev_valid_new_root_lineages"] == forced_valid,
            {"forced": forced_total, "valid": forced_valid},
        )
        selected = Path(arm_result["candidate_path"])
        check(
            f"{arm}_selected_candidate_hash",
            sha256_file(selected) == arm_result["candidate_sha256"],
            arm_result["candidate_sha256"],
        )
    radical = result["arms"]["radical_roots"]
    controller_repaired = bool(
        radical["timely_escape"]
        and all(
            not result["arms"][arm]["parent_guardrail_violations"]
            for arm in ARMS
        )
    )
    expected = (
        "CONTROLLER_NOT_REPAIRED"
        if not controller_repaired
        else "CONTROLLER_REPAIRED_OPERATOR_INSUFFICIENT"
        if not (
            radical["new_structural_root_lineages"] >= 2
            and radical["dev_valid_new_root_lineages"] >= 1
            and radical["basin_jump_rate"] >= 0.25
            and radical["basin_jump_rate_lift_vs_frozen"] >= 0.25
        )
        else "M2_R1_PASS"
        if radical["improved_vs_frozen_h50_dev"]
        else "CONTROLLER_AND_OPERATOR_REPAIRED_QUALITY_NOT_IMPROVED"
    )
    check(
        "mechanism_outcome_recomputed",
        result["mechanism_outcome"] == expected,
        expected,
    )
    validation = {
        "schema_version": "1.0",
        "result_sha256": sha256_file(result_path),
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }
    (HERE / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not validation["passed"]:
        raise SystemExit("M2-R1B validation failed")


if __name__ == "__main__":
    main()
