"""Build the MCP technical report for the M2-R0 parent-policy replay."""

from __future__ import annotations

import json
import pathlib
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
RESULT = json.loads((HERE / "result.json").read_text())
VALIDATION = json.loads((HERE / "validation.json").read_text())
OUTPUT = HERE / "artifact.json"


def source(sql: str, description: str, metrics: list[str]) -> dict[str, Any]:
    return {
        "id": "m2_r0_replay",
        "label": "Frozen Set Cover development-only parent-policy replay",
        "path": "research/results/algotune_set_cover_m2_r0_parent_replay_v0/result.json",
        "query": {
            "language": "sql",
            "sql": sql,
            "description": description,
            "tables_used": ["result.json"],
            "metric_definitions": metrics,
            "filters": [
                "frozen Set Cover development campaign only",
                "no blind artifacts",
                "no new candidates or evaluator/model calls",
            ],
        },
    }


BASE_SOURCE = source(
    "SELECT * FROM result_json",
    "Read the complete deterministic M2-R0 replay result.",
    [
        "parent exposure counts candidate appearances in the recorded or replayed two-slot parent pool",
        "quality ratio is candidate development speedup divided by the contemporaneous development incumbent",
        "root lineage follows the first genetic parent to its first valid ancestor",
    ],
)


def main() -> None:
    labels = {
        "observed": "Observed frozen policy",
        "incumbent_guardrail": "Incumbent guardrail",
        "guardrail_stagnation_escape": "Guardrail + escape",
    }
    policy_rows = []
    gen13_rows = []
    for row in RESULT["policy_summaries"]:
        policy_rows.append(
            {
                "policy": labels[row["policy_id"]],
                "parent_slots": row["candidate_parent_slots"],
                "gen13_slots": row["gen13_exposure"],
                "sub_guardrail_share": round(row["sub_guardrail_share"] or 0.0, 6),
                "mean_quality_ratio": round(row["mean_parent_quality_ratio"] or 0.0, 6),
                "max_parent_share": round(row["max_parent_share"], 6),
                "effective_parents": round(row["effective_parent_count"], 3),
                "root_lineages": row["distinct_root_lineages"],
                "escape_required_slots": row["escape_required_slots"],
            }
        )
        gen13_rows.append(
            {
                "policy": labels[row["policy_id"]],
                "gen13_parent_slots": row["gen13_exposure"],
            }
        )

    stagnation = RESULT["stagnation_observation"]
    response_rows = [
        {
            "detector": "Recorded controller",
            "first_response_generation": stagnation[
                "recorded_first_breakthrough_after_gen9"
            ],
        },
        {
            "detector": "Incumbent-refresh N=3",
            "first_response_generation": stagnation[
                "counterfactual_first_escape_generation_n3"
            ],
        },
    ]
    sensitivity_rows = [
        {
            "near_incumbent_ratio": row["near_incumbent_ratio"],
            "gen13_eligible_generations": row["gen13_eligible_generations"],
            "gen13_selected_slots": row["gen13_exposure"],
            "distinct_parents": row["distinct_parents"],
            "mean_quality_ratio": round(row["mean_parent_quality_ratio"], 6),
            "max_parent_share": round(row["max_parent_share"], 6),
        }
        for row in RESULT["near_incumbent_sensitivity"]
    ]

    manifest = {
        "version": 1,
        "title": "M2-R0 Set Cover Parent-Policy Replay",
        "surface": "report",
        "description": "Zero-token development-only counterfactual replay of EvidenceEvolve parent selection and stagnation escape.",
        "generatedAt": "2026-08-16T00:00:00Z",
        "sources": [BASE_SOURCE],
        "blocks": [
            {
                "id": "title",
                "type": "markdown",
                "body": "# M2-R0 Set Cover Parent-Policy Replay\nDeterministic, development-only replay. No new candidate, model call, evaluator call, or blind artifact was used.",
            },
            {
                "id": "technical_summary",
                "type": "markdown",
                "sourceId": "m2_r0_replay",
                "body": "## Technical summary\n**Result: parent-pressure failure confirmed; selection-only repair is necessary but insufficient.** The observed frozen pools used GEN-13 in 37 of 85 candidate parent slots even though it trailed the GEN-9 incumbent. A 0.5% incumbent guardrail reduces GEN-13 exposure to zero and eliminates all below-guardrail parent slots, raising mean selected-parent quality from 0.9881 to 0.9999 of the contemporaneous incumbent. But all 36 dev-valid candidates share one root lineage, so static selection cannot supply the missing basin jump. The correct R1 design therefore needs both a parent-quality repair and an explicit escape source.",
            },
            {
                "id": "key_findings",
                "type": "markdown",
                "body": "## Key findings with visual evidence\nThe replay separates two claims. The pressure claim is directly identified over the frozen candidate pool: GEN-13 need not occupy any parent slot under the preregistered guardrails. The downstream quality claim is not identified: changing parents would have changed future proposals, and those counterfactual candidates do not exist in this ledger.",
            },
            {"id": "gen13_chart_block", "type": "chart", "chartId": "gen13_exposure"},
            {"id": "policy_table_block", "type": "table", "tableId": "policy_summary"},
            {
                "id": "stagnation",
                "type": "markdown",
                "body": "## Stagnation detector diagnosis\nGEN-9 is the last incumbent refresh. An incumbent-refresh counter with N=3 requires escape at GEN-13. The frozen controller does not enter its next recorded `BREAKTHROUGH` mode until GEN-41 (then GEN-42 and GEN-43), because its state is not equivalent to a strict incumbent-refresh clock. At GEN-13 the frozen pool contains no different root lineage, so the replay emits `ESCAPE_REQUIRED` rather than pretending that another same-lineage candidate is a basin jump.",
            },
            {"id": "stagnation_chart_block", "type": "chart", "chartId": "stagnation_response"},
            {
                "id": "scope",
                "type": "markdown",
                "body": "## Scope, data, and definitions\nThe input is the frozen 50-generation Set Cover development campaign: recorded parent pools, the M1 generation ledger, population novelty/information-gain fields, and first-parent ancestry. `Observed` reproduces the stored policy-effect traces. `Incumbent guardrail` restricts preferred parents to candidates within 0.5% of the contemporaneous incumbent and ranks by dev quality, novelty, information gain, then ID. `Guardrail + escape` reserves a second slot for a different root lineage after three strict non-improving generations; when none exists it records `ESCAPE_REQUIRED`.",
            },
            {
                "id": "methodology",
                "type": "markdown",
                "body": "## Methodology\nA deterministic script reconstructs the parent-eligible state before each generation, recomputes the incumbent chronologically, and replays three preregistered policies without modifying the archive. Metrics are parent-slot exposure, quality relative to the contemporaneous incumbent, concentration (maximum share and HHI), effective parent count, root-lineage count, and escape timing. An independent validator re-reads all 50 policy traces and the M1 ledger, recomputes observed exposure and concentration, and checks the causal-boundary flags.",
            },
            {
                "id": "robustness",
                "type": "markdown",
                "body": "## Limitations and robustness\nThe GEN-13 non-selection result holds across preregistered near-incumbent ratios from 1.00 to 0.98; at 0.98 GEN-13 becomes eligible for 37 generations but is still never selected among the top two. Guardrails alone reduce low-quality pressure but increase concentration, so they do not solve diversity. Root lineage is a structural first-parent definition, not a semantic algorithm-family classifier. Most importantly, this replay cannot estimate which candidates a repaired controller would generate or whether final development or held-out quality would improve. Validation status: `READY_TO_SHARE_WITH_CAVEATS`.",
            },
            {"id": "sensitivity_table_block", "type": "table", "tableId": "ratio_sensitivity"},
            {
                "id": "next_steps",
                "type": "markdown",
                "body": "## Recommended M2-R1 decision\nProceed with a small fixed-budget development experiment, not blind evaluation: (1) frozen baseline EE, (2) parent-pressure repair, (3) repair plus incumbent-based stagnation/diversity escape, and (4) repair plus an explicit radical or cross-lineage source. Keep validity/admission, parent eligibility, and preferred-parent ranking as separate states. Predeclare pass criteria requiring both final quality and search-dynamics improvement: incumbent refreshes, productive lineages, recovery after stagnation, parent concentration, novelty, and improvement per token/wall time. Do not treat a higher final dev score alone as M2 PASS.",
            },
            {
                "id": "questions",
                "type": "markdown",
                "body": "## Further questions\n1. Does a semantic mechanism-family classifier reveal more than one family inside the single structural root lineage? 2. What minimum escape quota yields new productive roots without overwhelming exploitation? 3. Does the guardrail need a concentration cap or exposure decay to prevent GEN-9 from becoming the next dominant parent? 4. Can cross-lineage context be introduced without turning development validation into a second fitness surface?",
            },
        ],
        "charts": [
            {
                "id": "gen13_exposure",
                "title": "GEN-13 parent-slot exposure by replay policy",
                "subtitle": "Both preregistered repairs remove GEN-13 from preferred parent slots in the frozen pool.",
                "type": "bar",
                "source": source(
                    "SELECT policy, gen13_parent_slots FROM gen13_exposure ORDER BY gen13_parent_slots DESC",
                    "Compare GEN-13 parent-slot exposure across observed and replayed policies.",
                    ["GEN-13 exposure is the number of candidate parent slots occupied by GEN-013-C01"],
                ),
                "dataset": "gen13_exposure",
                "encodings": {
                    "x": {"field": "policy", "type": "nominal", "title": "Policy"},
                    "y": {"field": "gen13_parent_slots", "type": "quantitative", "title": "Parent slots"},
                },
                "options": {"orientation": "vertical"},
            },
            {
                "id": "stagnation_response",
                "title": "First post-GEN-9 stagnation response generation",
                "subtitle": "An incumbent-refresh clock would demand escape 28 generations earlier than the recorded controller response.",
                "type": "bar",
                "source": source(
                    "SELECT detector, first_response_generation FROM stagnation_response ORDER BY first_response_generation DESC",
                    "Compare the recorded breakthrough timing with a strict three-generation incumbent-refresh detector.",
                    ["response generation is the first generation after GEN-9 at which the detector requires or records escape"],
                ),
                "dataset": "stagnation_response",
                "encodings": {
                    "x": {"field": "detector", "type": "nominal", "title": "Detector"},
                    "y": {"field": "first_response_generation", "type": "quantitative", "title": "Generation"},
                },
                "options": {"orientation": "vertical"},
            },
        ],
        "tables": [
            {
                "id": "policy_summary",
                "title": "Parent-policy replay summary",
                "source": BASE_SOURCE,
                "dataset": "policy_summary",
                "columns": [
                    {"field": "policy", "label": "Policy", "type": "text"},
                    {"field": "parent_slots", "label": "Candidate slots", "type": "number"},
                    {"field": "gen13_slots", "label": "GEN-13 slots", "type": "number"},
                    {"field": "sub_guardrail_share", "label": "Below guardrail", "type": "percent"},
                    {"field": "mean_quality_ratio", "label": "Mean quality/incumbent", "type": "number"},
                    {"field": "max_parent_share", "label": "Max parent share", "type": "percent"},
                    {"field": "effective_parents", "label": "Effective parents", "type": "number"},
                    {"field": "root_lineages", "label": "Root lineages", "type": "number"},
                    {"field": "escape_required_slots", "label": "Escape required", "type": "number"},
                ],
                "defaultSort": {"field": "gen13_slots", "direction": "desc"},
            },
            {
                "id": "ratio_sensitivity",
                "title": "Near-incumbent guardrail sensitivity",
                "source": BASE_SOURCE,
                "dataset": "ratio_sensitivity",
                "columns": [
                    {"field": "near_incumbent_ratio", "label": "Guardrail ratio", "type": "percent"},
                    {"field": "gen13_eligible_generations", "label": "GEN-13 eligible generations", "type": "number"},
                    {"field": "gen13_selected_slots", "label": "GEN-13 selected slots", "type": "number"},
                    {"field": "distinct_parents", "label": "Distinct parents", "type": "number"},
                    {"field": "mean_quality_ratio", "label": "Mean quality/incumbent", "type": "number"},
                    {"field": "max_parent_share", "label": "Max parent share", "type": "percent"},
                ],
                "defaultSort": {"field": "near_incumbent_ratio", "direction": "desc"},
            },
        ],
    }

    snapshot = {
        "version": 1,
        "status": "ready",
        "generatedAt": "2026-08-16T00:00:00Z",
        "datasets": {
            "policy_summary": policy_rows,
            "gen13_exposure": gen13_rows,
            "stagnation_response": response_rows,
            "ratio_sensitivity": sensitivity_rows,
            "validation": [
                {
                    "status": VALIDATION["status"],
                    "failed_checks": len(VALIDATION["failures"]),
                    "result_sha256": VALIDATION["result_sha256"],
                }
            ],
        },
    }
    OUTPUT.write_text(json.dumps({"manifest": manifest, "snapshot": snapshot}, indent=2) + "\n")


if __name__ == "__main__":
    main()
