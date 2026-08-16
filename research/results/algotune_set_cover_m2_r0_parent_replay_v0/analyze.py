"""M2-R0 zero-token counterfactual parent-policy replay.

Only frozen Set Cover development campaign artifacts are read. The replay does
not generate candidates, call a model/evaluator, or inspect blind artifacts.
"""

from __future__ import annotations

import collections
import json
import math
import pathlib
import sqlite3
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
PROTOCOL = json.loads((HERE / "protocol.json").read_text())
CAMPAIGN = ROOT / PROTOCOL["source_campaign"]
M1_RESULT = (
    ROOT
    / "research/results/algotune_set_cover_mechanism_autopsy_v0/result.json"
)
OUTPUT = HERE / "result.json"


def load_json(path: pathlib.Path) -> dict[str, Any]:
    resolved = path.resolve()
    if "blind" in resolved.as_posix().lower():
        raise RuntimeError(f"prohibited blind path: {resolved}")
    return json.loads(path.read_text())


def generation(candidate_id: str) -> int:
    return int(candidate_id[4:7])


def candidate_records() -> dict[str, dict[str, Any]]:
    m1 = load_json(M1_RESULT)["arm_observability"]["evidence_evolve"]
    rows = {row["candidate_id"]: dict(row) for row in m1["generation_rows"]}
    connection = sqlite3.connect(CAMPAIGN / "research.db")
    connection.row_factory = sqlite3.Row
    population = {
        row["candidate_id"]: dict(row)
        for row in connection.execute("select * from population_candidates")
    }
    for candidate_id, row in rows.items():
        if candidate_id not in population:
            continue
        stored = population[candidate_id]
        row.update(
            {
                "information_gain": float(stored["information_gain"]),
                "novelty": float(stored["novelty"]),
                "acquisition_score": (
                    None
                    if stored["acquisition_score"] is None
                    else float(stored["acquisition_score"])
                ),
                "behavior_key": stored["behavior_key"],
            }
        )
    return rows


def lineage_roots(records: dict[str, dict[str, Any]]) -> dict[str, str]:
    cache: dict[str, str] = {}

    def root(candidate_id: str, stack: set[str] | None = None) -> str:
        if candidate_id in cache:
            return cache[candidate_id]
        if candidate_id == "SEED" or candidate_id not in records:
            return "SEED"
        stack = set() if stack is None else set(stack)
        if candidate_id in stack:
            raise ValueError(f"lineage cycle at {candidate_id}")
        stack.add(candidate_id)
        parents = records[candidate_id].get("parent_ids") or ["SEED"]
        genetic_parent = parents[0]
        if genetic_parent == "SEED":
            value = candidate_id
        else:
            value = root(genetic_parent, stack)
        cache[candidate_id] = value
        return value

    for candidate_id in records:
        root(candidate_id)
    return cache


def observed_traces() -> dict[int, dict[str, Any]]:
    traces: dict[int, dict[str, Any]] = {}
    for index in range(1, 51):
        path = CAMPAIGN / "generations" / f"GEN-{index:03d}" / "policy_effect_trace.json"
        traces[index] = load_json(path)
    return traces


def observed_pools(traces: dict[int, dict[str, Any]]) -> dict[int, list[str]]:
    pools: dict[int, list[str]] = {}
    for index, trace in traces.items():
        pools[index] = list(trace["parent_pools_by_island"].get("main", []))
    return pools


def incumbent_state(records: dict[str, dict[str, Any]]) -> tuple[dict[int, float], dict[int, int]]:
    baseline = 0.9987262059725405
    incumbent = baseline
    streak = 0
    values: dict[int, float] = {}
    streaks: dict[int, int] = {}
    by_generation = {generation(candidate_id): row for candidate_id, row in records.items()}
    for index in range(1, 51):
        values[index] = incumbent
        streaks[index] = streak
        row = by_generation.get(index)
        if row and row.get("valid") and row.get("raw_speedup") is not None:
            score = float(row["raw_speedup"])
            if score > incumbent:
                incumbent = score
                streak = 0
                continue
        streak += 1
    return values, streaks


def quality_key(candidate: dict[str, Any]) -> tuple[object, ...]:
    return (
        float(candidate["raw_speedup"]),
        float(candidate.get("novelty", 0.0)),
        float(candidate.get("information_gain", 0.0)),
        candidate["candidate_id"],
    )


def balanced_key(
    candidate: dict[str, Any],
    pool: list[dict[str, Any]],
    exposure: collections.Counter[str],
) -> tuple[object, ...]:
    def percentile(field: str, value: float) -> float:
        values = sorted(float(item.get(field, 0.0)) for item in pool)
        if len(values) <= 1:
            return 1.0
        below = sum(item < value for item in values)
        equal = sum(item == value for item in values)
        return (below + 0.5 * equal) / len(values)

    score = (
        percentile("raw_speedup", float(candidate["raw_speedup"]))
        + percentile("novelty", float(candidate.get("novelty", 0.0)))
        + percentile("information_gain", float(candidate.get("information_gain", 0.0)))
        + 1.0 / (1.0 + exposure[candidate["candidate_id"]])
    ) / 4.0
    return (score, *quality_key(candidate))


def guardrail_pools(
    records: dict[str, dict[str, Any]],
    incumbents: dict[int, float],
    *,
    near_incumbent_ratio: float,
) -> dict[int, list[str]]:
    pools: dict[int, list[str]] = {}
    for index in range(1, 51):
        available = [
            row
            for candidate_id, row in records.items()
            if row.get("valid")
            and generation(candidate_id) < index
            and float(row["raw_speedup"]) >= near_incumbent_ratio * incumbents[index]
        ]
        pools[index] = [
            row["candidate_id"]
            for row in sorted(available, key=quality_key, reverse=True)[:2]
        ] or ["SEED"]
    return pools


def escape_pools(
    records: dict[str, dict[str, Any]],
    roots: dict[str, str],
    incumbents: dict[int, float],
    streaks: dict[int, int],
    *,
    near_incumbent_ratio: float,
    stagnation_generations: int,
) -> dict[int, list[str]]:
    pools: dict[int, list[str]] = {}
    exposure: collections.Counter[str] = collections.Counter()
    for index in range(1, 51):
        available = [
            row
            for candidate_id, row in records.items()
            if row.get("valid") and generation(candidate_id) < index
        ]
        near = [
            row
            for row in available
            if float(row["raw_speedup"]) >= near_incumbent_ratio * incumbents[index]
        ]
        if not near:
            pools[index] = ["SEED"]
            continue
        champion = max(near, key=quality_key)
        selected = [champion["candidate_id"]]
        if streaks[index] >= stagnation_generations:
            alternatives = [
                row
                for row in available
                if roots[row["candidate_id"]] != roots[champion["candidate_id"]]
            ]
            if alternatives:
                selected.append(
                    max(
                        alternatives,
                        key=lambda row: balanced_key(row, alternatives, exposure),
                    )["candidate_id"]
                )
            else:
                selected.append("ESCAPE_REQUIRED")
        else:
            alternatives = [row for row in near if row["candidate_id"] != champion["candidate_id"]]
            if alternatives:
                selected.append(max(alternatives, key=quality_key)["candidate_id"])
        pools[index] = selected
        exposure.update(candidate_id for candidate_id in selected if candidate_id in records)
    return pools


def summarize_policy(
    policy_id: str,
    pools: dict[int, list[str]],
    records: dict[str, dict[str, Any]],
    roots: dict[str, str],
    incumbents: dict[int, float],
    *,
    guardrail_ratio: float = 0.995,
) -> dict[str, Any]:
    exposure: collections.Counter[str] = collections.Counter()
    quality_ratios: list[float] = []
    sub_guardrail = 0
    escape_slots = 0
    first_escape: int | None = None
    rows: list[dict[str, Any]] = []
    for index in range(1, 51):
        chosen = pools.get(index, [])
        for candidate_id in chosen:
            if candidate_id == "ESCAPE_REQUIRED":
                escape_slots += 1
                if first_escape is None:
                    first_escape = index
                rows.append(
                    {
                        "generation": index,
                        "slot": candidate_id,
                        "raw_speedup": None,
                        "incumbent": incumbents[index],
                        "quality_ratio": None,
                        "lineage_root": None,
                    }
                )
                continue
            if candidate_id == "SEED" or candidate_id not in records:
                continue
            row = records[candidate_id]
            ratio = float(row["raw_speedup"]) / incumbents[index]
            exposure[candidate_id] += 1
            quality_ratios.append(ratio)
            if ratio < guardrail_ratio:
                sub_guardrail += 1
            rows.append(
                {
                    "generation": index,
                    "slot": candidate_id,
                    "raw_speedup": float(row["raw_speedup"]),
                    "incumbent": incumbents[index],
                    "quality_ratio": ratio,
                    "lineage_root": roots[candidate_id],
                }
            )
    candidate_slots = sum(exposure.values())
    shares = [count / candidate_slots for count in exposure.values()] if candidate_slots else []
    hhi = sum(share * share for share in shares)
    root_ids = {roots[candidate_id] for candidate_id in exposure}
    return {
        "policy_id": policy_id,
        "candidate_parent_slots": candidate_slots,
        "escape_required_slots": escape_slots,
        "first_escape_generation": first_escape,
        "distinct_parents": len(exposure),
        "distinct_root_lineages": len(root_ids),
        "max_parent_share": max(shares, default=0.0),
        "parent_hhi": hhi,
        "effective_parent_count": (1.0 / hhi if hhi else 0.0),
        "mean_parent_quality_ratio": (
            sum(quality_ratios) / len(quality_ratios) if quality_ratios else None
        ),
        "sub_guardrail_slots": sub_guardrail,
        "sub_guardrail_share": (
            sub_guardrail / candidate_slots if candidate_slots else None
        ),
        "gen13_exposure": exposure["GEN-013-C01"],
        "gen9_exposure": exposure["GEN-009-C01"],
        "parent_exposure": dict(exposure.most_common()),
        "slot_rows": rows,
    }


def main() -> None:
    records = candidate_records()
    roots = lineage_roots(records)
    incumbents, streaks = incumbent_state(records)
    traces = observed_traces()
    observed = observed_pools(traces)
    guardrail = guardrail_pools(
        records,
        incumbents,
        near_incumbent_ratio=0.995,
    )
    escape = escape_pools(
        records,
        roots,
        incumbents,
        streaks,
        near_incumbent_ratio=0.995,
        stagnation_generations=3,
    )
    policies = [
        summarize_policy("observed", observed, records, roots, incumbents),
        summarize_policy(
            "incumbent_guardrail", guardrail, records, roots, incumbents
        ),
        summarize_policy(
            "guardrail_stagnation_escape", escape, records, roots, incumbents
        ),
    ]

    ratio_sensitivity = []
    for ratio in PROTOCOL["sensitivity"]["near_incumbent_ratios"]:
        gen13_eligible_generations = sum(
            1
            for index in range(14, 51)
            if float(records["GEN-013-C01"]["raw_speedup"])
            >= ratio * incumbents[index]
        )
        summary = summarize_policy(
            f"guardrail_{ratio:.3f}",
            guardrail_pools(records, incumbents, near_incumbent_ratio=ratio),
            records,
            roots,
            incumbents,
            guardrail_ratio=ratio,
        )
        ratio_sensitivity.append(
            {
                "near_incumbent_ratio": ratio,
                "gen13_eligible_generations": gen13_eligible_generations,
                "gen13_exposure": summary["gen13_exposure"],
                "mean_parent_quality_ratio": summary["mean_parent_quality_ratio"],
                "distinct_parents": summary["distinct_parents"],
                "max_parent_share": summary["max_parent_share"],
            }
        )

    stagnation_sensitivity = []
    for threshold in PROTOCOL["sensitivity"]["stagnation_generations"]:
        summary = summarize_policy(
            f"escape_{threshold}",
            escape_pools(
                records,
                roots,
                incumbents,
                streaks,
                near_incumbent_ratio=0.995,
                stagnation_generations=threshold,
            ),
            records,
            roots,
            incumbents,
        )
        stagnation_sensitivity.append(
            {
                "stagnation_generations": threshold,
                "first_escape_generation": summary["first_escape_generation"],
                "escape_required_slots": summary["escape_required_slots"],
                "gen13_exposure": summary["gen13_exposure"],
            }
        )

    valid_records = [row for row in records.values() if row.get("valid")]
    unique_roots = sorted({roots[row["candidate_id"]] for row in valid_records})
    recorded_breakthrough_generations = [
        index for index, trace in traces.items() if trace["mode"] == "BREAKTHROUGH"
    ]
    result = {
        "schema_version": "1.0",
        "study_id": PROTOCOL["study_id"],
        "scope": PROTOCOL["scope"],
        "blind_artifacts_read": False,
        "new_candidates": 0,
        "model_calls": 0,
        "evaluator_calls": 0,
        "candidate_population": {
            "development_valid_candidates": len(valid_records),
            "unique_root_lineages": len(unique_roots),
            "root_lineage_ids": unique_roots,
            "best_frozen_candidate": max(valid_records, key=quality_key)["candidate_id"],
            "best_frozen_raw_speedup": max(float(row["raw_speedup"]) for row in valid_records),
        },
        "stagnation_observation": {
            "incumbent_last_refresh_generation": 9,
            "counterfactual_first_escape_generation_n3": 13,
            "recorded_breakthrough_generations": recorded_breakthrough_generations,
            "recorded_first_breakthrough_after_gen9": next(
                (index for index in recorded_breakthrough_generations if index > 9),
                None,
            ),
            "definition_note": (
                "Counterfactual stagnation resets only on strict incumbent refresh; "
                "recorded campaign mode follows the frozen controller state."
            ),
        },
        "policy_summaries": policies,
        "near_incumbent_sensitivity": ratio_sensitivity,
        "stagnation_sensitivity": stagnation_sensitivity,
        "generation_state": [
            {
                "generation": index,
                "incumbent": incumbents[index],
                "stagnation_before_generation": streaks[index],
                "observed_pool": observed[index],
                "guardrail_pool": guardrail[index],
                "escape_pool": escape[index],
            }
            for index in range(1, 51)
        ],
        "identified_findings": [
            "A 0.5% incumbent guardrail can be evaluated exactly over the frozen candidate pool.",
            "The frozen development-valid pool contains only one root lineage, so no parent-only replay can create cross-basin diversity.",
            "All replay policies have the same best frozen candidate because no new candidate is generated.",
        ],
        "non_identified_claims": [
            "Counterfactual candidates that would have been generated from different parents.",
            "Counterfactual final development or held-out quality under a changed parent policy.",
            "The causal effect of a radical operator, which requires a new fixed-budget development experiment.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
