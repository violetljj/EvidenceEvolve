from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from evidence_evolve.hashing import sha256_file, sha256_object


ROOT = Path(__file__).resolve().parents[3]
RUN_ROOT = ROOT / "runs/algotune_set_cover_m2_r1b_dev_v1"
OUT = Path(__file__).resolve().parent
R0 = ROOT / "research/results/algotune_set_cover_m2_r0_parent_replay_v0/result.json"
R1A = ROOT / "research/results/algotune_set_cover_m2_r1a_controller_replay_v0/result.json"
ARMS = ("controller_only", "radical_roots")
ESCAPE_MUTATIONS = {"representation_mutation", "cross_family", "restart"}


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tree_hash(root: Path) -> str:
    return sha256_object(
        {
            str(path.relative_to(root)): sha256_file(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
    )


def _receipt(campaign: Path, candidate_id: str) -> dict[str, Any] | None:
    receipt_dir = campaign / "candidates" / candidate_id / "receipts"
    paths = sorted(
        path
        for path in receipt_dir.glob("*.json")
        if not path.name.endswith(".mechanism.json")
    )
    return _read(paths[0])["receipt"] if paths else None


def _arm(arm: str, frozen_best: float) -> dict[str, Any]:
    arm_dir = RUN_ROOT / arm
    campaign = arm_dir / "campaign"
    arm_result = _read(arm_dir / "arm_result.json")
    generation_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    root_by_candidate: dict[str, str] = {"SEED": "SEED"}
    parent_exposure: Counter[str] = Counter()
    families: set[str] = set()
    escape_trigger_generations: list[int] = []
    mechanism_violations: list[str] = []
    guardrail_violations: list[str] = []

    for generation in range(1, 17):
        generation_id = f"GEN-{generation:03d}"
        candidate_id = f"{generation_id}-C01"
        generation_dir = campaign / "generations" / generation_id
        state = _read(generation_dir / "m2_controller_state.json")
        mode = state["mode"]
        mutation = state["mutation_assignment"]
        if state["escape_triggered"]:
            escape_trigger_generations.append(generation)
        if state["stagnant_generations_before"] >= 3 and mode != "BREAKTHROUGH":
            mechanism_violations.append(f"{generation_id}:late_escape")
        if mode == "BREAKTHROUGH" and mutation not in ESCAPE_MUTATIONS:
            mechanism_violations.append(f"{generation_id}:local_escape_operator")
        for parent in state["parent_pool"]:
            parent_exposure[parent] += 1
        incumbent = float(state["incumbent_value_before"])
        tolerance = max(abs(incumbent), 1e-12) * 0.005
        for parent in state["preferred_parent_ids"]:
            value = state["objective_values"].get(parent)
            if value is None or float(value) < incumbent - tolerance:
                guardrail_violations.append(f"{generation_id}:{parent}")
        intentional_seed_restart = bool(
            mode == "BREAKTHROUGH"
            and mutation == "restart"
            and arm == "radical_roots"
        )
        if (
            state["parent_pool"] == ["SEED"]
            and state["objective_values"]
            and not intentional_seed_restart
        ):
            guardrail_violations.append(f"{generation_id}:unexpected_seed_fallback")

        proposal_path = generation_dir / "proposals" / f"{candidate_id}.json"
        proposal = _read(proposal_path) if proposal_path.is_file() else None
        genome = proposal["acquisition"]["candidate"] if proposal else None
        parent = str(genome["genetic_parent_id"]) if genome else None
        root = None
        family = None
        if genome:
            root = candidate_id if parent == "SEED" else root_by_candidate.get(parent, parent)
            root_by_candidate[candidate_id] = root
            family = str(genome["family"])
            families.add(family)
        receipt = _receipt(campaign, candidate_id)
        evaluation = receipt["evaluation_input"] if receipt else None
        valid = bool(
            evaluation
            and evaluation["mechanics_status"] == "PASS"
            and evaluation["data_eligible"]
            and not evaluation["protocol_violations"]
            and evaluation["controls"].get("candidate_valid")
            and evaluation["controls"].get("development_only")
        )
        speedup = (
            float(evaluation["metrics"]["raw_speedup"])
            if evaluation and "raw_speedup" in evaluation["metrics"]
            else None
        )
        forced_root = bool(
            genome
            and mode == "BREAKTHROUGH"
            and mutation == "restart"
            and parent == "SEED"
        )
        candidate_rows.append(
            {
                "generation": generation,
                "candidate_id": candidate_id,
                "mode": mode,
                "mutation": mutation,
                "genetic_parent_id": parent,
                "root_lineage_id": root,
                "family": family,
                "forced_seed_root": forced_root,
                "development_valid": valid,
                "raw_speedup": speedup,
                "scientific_outcome": (
                    evaluation["scientific_outcome"] if evaluation else None
                ),
            }
        )
        generation_rows.append(
            {
                "generation": generation,
                "mode": mode,
                "stagnant_generations_before": state["stagnant_generations_before"],
                "escape_budget_remaining_before": state[
                    "escape_budget_remaining_before"
                ],
                "escape_triggered": state["escape_triggered"],
                "mutation": mutation,
                "parent_pool": state["parent_pool"],
                "incumbent_value_before": incumbent,
            }
        )

    forced = [row for row in candidate_rows if row["forced_seed_root"]]
    valid_forced = [row for row in forced if row["development_valid"]]
    valid_rows = [row for row in candidate_rows if row["development_valid"]]
    measured = [float(row["raw_speedup"]) for row in valid_rows]
    best_observed = max(measured, default=float(arm_result["development"]["metrics"]["raw_speedup"]))
    max_parent_exposure = max(parent_exposure.values(), default=0)
    total_parent_exposure = sum(parent_exposure.values())
    basin_jump_rate = len(valid_forced) / len(forced) if forced else 0.0
    return {
        "policy_id": arm_result["metadata"]["policy_id"],
        "timely_escape": not mechanism_violations,
        "escape_trigger_generations": escape_trigger_generations,
        "breakthrough_generations": sum(
            row["mode"] == "BREAKTHROUGH" for row in generation_rows
        ),
        "protected_escape_or_timing_violations": mechanism_violations,
        "parent_guardrail_violations": guardrail_violations,
        "new_structural_root_lineages": len(forced),
        "dev_valid_new_root_lineages": len(valid_forced),
        "basin_jump_rate": basin_jump_rate,
        "basin_jump_rate_lift_vs_frozen": basin_jump_rate,
        "unique_observed_families": len(families),
        "development_valid_candidates": len(valid_rows),
        "best_observed_dev_speedup": best_observed,
        "selected_dev_speedup": float(
            arm_result["development"]["metrics"]["raw_speedup"]
        ),
        "improved_vs_frozen_h50_dev": best_observed > frozen_best,
        "tokens": int(arm_result["tokens"]),
        "wall_seconds": float(arm_result["wall_seconds"]),
        "improvement_per_100k_tokens": (
            (best_observed - frozen_best) / int(arm_result["tokens"]) * 100_000
            if int(arm_result["tokens"]) > 0
            else None
        ),
        "max_parent_exposure_share": (
            max_parent_exposure / total_parent_exposure
            if total_parent_exposure
            else 0.0
        ),
        "generation_rows": generation_rows,
        "candidate_rows": candidate_rows,
        "run_tree_sha256": _tree_hash(arm_dir),
        "blind_artifacts_read": arm_result["metadata"]["blind_artifacts_read"],
        "blind_evaluator_calls": arm_result["metadata"]["blind_evaluator_calls"],
    }


def main() -> None:
    frozen = _read(R0)["candidate_population"]
    frozen_best = float(frozen["best_frozen_raw_speedup"])
    arms = {arm: _arm(arm, frozen_best) for arm in ARMS}
    radical = arms["radical_roots"]
    timing = bool(radical["timely_escape"])
    controller_repaired = bool(
        timing
        and all(not row["parent_guardrail_violations"] for row in arms.values())
    )
    operator = bool(
        radical["new_structural_root_lineages"] >= 2
        and radical["dev_valid_new_root_lineages"] >= 1
        and radical["basin_jump_rate"] >= 0.25
        and radical["basin_jump_rate_lift_vs_frozen"] >= 0.25
    )
    full_pass = bool(
        controller_repaired and operator and radical["improved_vs_frozen_h50_dev"]
    )
    if not controller_repaired:
        outcome = "CONTROLLER_NOT_REPAIRED"
    elif not operator:
        outcome = "CONTROLLER_REPAIRED_OPERATOR_INSUFFICIENT"
    elif full_pass:
        outcome = "M2_R1_PASS"
    else:
        outcome = "CONTROLLER_AND_OPERATOR_REPAIRED_QUALITY_NOT_IMPROVED"
    result = {
        "schema_version": "1.0",
        "study_id": "algotune_set_cover_m2_r1b_escape_v0",
        "scope": "PROSPECTIVE_SET_COVER_DEVELOPMENT_ONLY_MECHANISM_TEST",
        "blind_artifacts_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
        "historical_reference": {
            "frozen_h50_best_candidate": frozen["best_frozen_candidate"],
            "frozen_h50_best_dev_speedup": frozen_best,
            "frozen_unique_root_lineages": frozen["unique_root_lineages"],
            "frozen_basin_jump_rate": 0.0,
        },
        "r1a": {
            "result_sha256": sha256_file(R1A),
            "first_post_gen9_escape": _read(R1A)["repaired_first_escape_after_gen9"],
            "escape_timing_gain_generations": _read(R1A)[
                "escape_timing_gain_generations"
            ],
        },
        "arms": arms,
        "primary_arm": "radical_roots",
        "criteria": {
            "timely_escape": timing,
            "parent_guardrail_preserved": all(
                not row["parent_guardrail_violations"] for row in arms.values()
            ),
            "new_structural_root_lineages_min_2": radical[
                "new_structural_root_lineages"
            ] >= 2,
            "dev_valid_new_root_lineages_min_1": radical[
                "dev_valid_new_root_lineages"
            ] >= 1,
            "basin_jump_rate_min_0_25": radical["basin_jump_rate"] >= 0.25,
            "basin_jump_rate_lift_min_0_25": radical[
                "basin_jump_rate_lift_vs_frozen"
            ] >= 0.25,
            "final_incumbent_improved": radical["improved_vs_frozen_h50_dev"],
        },
        "mechanism_outcome": outcome,
        "full_pass": full_pass,
        "interpretation": (
            "The repaired controller generated and validated new forced-SEED root "
            "lineages under the frozen development evaluator. This is development-only "
            "mechanism evidence and does not establish blind generalization."
            if operator
            else "The strict incumbent clock worked, but the radical operator did not "
            "meet the preregistered new-root validity threshold."
        ),
        "limitations": [
            "No Set Cover blind instance, blind evaluator, or confirmation asset was read.",
            "A forced SEED Git root is a scheduling/mechanics lineage definition, not proof of semantic novelty.",
            "Development speedup is vulnerable to development overfitting and is not a held-out claim.",
            "The two prospective arms are single trajectories, so stochastic uncertainty is not estimated.",
        ],
    }
    (OUT / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
