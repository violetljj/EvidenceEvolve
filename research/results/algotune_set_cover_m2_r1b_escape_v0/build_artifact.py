"""Build the validated MCP technical report payload for M2-R1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
RESULT = json.loads((HERE / "result.json").read_text(encoding="utf-8"))
VALIDATION = json.loads((HERE / "validation.json").read_text(encoding="utf-8"))


def source(sql: str, description: str, definitions: list[str]) -> dict[str, Any]:
    return {
        "id": "m2_r1_dev",
        "label": "M2-R1 prospective Set Cover development-only result",
        "path": "research/results/algotune_set_cover_m2_r1b_escape_v0/result.json",
        "query": {
            "language": "sql",
            "sql": sql,
            "description": description,
            "tables_used": ["result.json"],
            "metric_definitions": definitions,
            "filters": [
                "two preregistered 16-generation prospective arms",
                "frozen Set Cover development evaluator only",
                "zero blind evaluator and confirmation calls",
            ],
        },
    }


def main() -> None:
    arms = RESULT["arms"]
    radical = arms["radical_roots"]
    controller = arms["controller_only"]
    outcome = RESULT["mechanism_outcome"]
    arm_rows = [
        {
            "arm": arm,
            "timely_escape": row["timely_escape"],
            "new_roots": row["new_structural_root_lineages"],
            "valid_new_roots": row["dev_valid_new_root_lineages"],
            "basin_jump_rate": round(row["basin_jump_rate"], 6),
            "valid_candidates": row["development_valid_candidates"],
            "best_dev_speedup": round(row["best_observed_dev_speedup"], 6),
            "tokens": row["tokens"],
            "wall_seconds": round(row["wall_seconds"], 3),
            "max_parent_share": round(row["max_parent_exposure_share"], 6),
        }
        for arm, row in arms.items()
    ]
    root_rows = [
        {"arm": arm, "metric": metric, "count": row[key]}
        for arm, row in arms.items()
        for metric, key in (
            ("Forced new roots", "new_structural_root_lineages"),
            ("Dev-valid new roots", "dev_valid_new_root_lineages"),
        )
    ]
    performance_rows = [
        {
            "series": "Frozen EE h50 reference",
            "dev_speedup": RESULT["historical_reference"][
                "frozen_h50_best_dev_speedup"
            ],
        },
        {
            "series": "M2 controller-only best",
            "dev_speedup": controller["best_observed_dev_speedup"],
        },
        {
            "series": "M2 radical-roots best",
            "dev_speedup": radical["best_observed_dev_speedup"],
        },
    ]
    timeline_rows = [
        {
            "arm": arm,
            "generation": row["generation"],
            "incumbent_before": row["incumbent_value_before"],
            "mode": row["mode"],
        }
        for arm, arm_result in arms.items()
        for row in arm_result["generation_rows"]
    ]
    base_source = source(
        "SELECT * FROM result_json",
        "Read the independently validated M2-R1 development-only mechanism result.",
        [
            "new structural root is a BREAKTHROUGH restart whose genetic parent is SEED",
            "basin-jump rate is dev-valid forced-SEED roots divided by forced-SEED restart proposals",
            "timely escape requires BREAKTHROUGH whenever strict pre-generation stagnation is at least three",
        ],
    )
    manifest = {
        "version": 1,
        "title": "M2-R1 Set Cover Escape Mechanism Repair",
        "surface": "report",
        "description": "Prospective fixed-budget development-only causal test of strict stagnation timing and radical root generation.",
        "generatedAt": "2026-08-16T00:00:00Z",
        "sources": [base_source],
        "blocks": [
            {
                "id": "title",
                "type": "markdown",
                "body": "# M2-R1 Set Cover Escape Mechanism Repair\nProspective, fixed-budget, development-only mechanism experiment. No blind or confirmation call was made.",
            },
            {
                "id": "summary",
                "type": "markdown",
                "sourceId": "m2_r1_dev",
                "body": (
                    "## Decision\n"
                    f"**Preregistered mechanism outcome: `{outcome}`.** The radical arm "
                    f"triggered escape on time, generated {radical['new_structural_root_lineages']} "
                    f"forced-SEED roots, and produced {radical['dev_valid_new_root_lineages']} "
                    f"dev-valid new roots (basin-jump rate {radical['basin_jump_rate']:.1%}). "
                    f"Its best observed development speedup was {radical['best_observed_dev_speedup']:.3f}× "
                    f"versus the frozen EE h50 development reference of "
                    f"{RESULT['historical_reference']['frozen_h50_best_dev_speedup']:.3f}×."
                ),
            },
            {
                "id": "roots_intro",
                "type": "markdown",
                "body": "## Escape mechanics\nThe controller-only arm isolates timing, guardrail, and protected structural operators without forced Git-root creation. The radical arm adds forced SEED restarts. Root counts below are mechanics lineage facts; they do not claim semantic novelty or blind generalization.",
            },
            {"id": "roots_chart_block", "type": "chart", "chartId": "root_outcomes"},
            {"id": "arm_table_block", "type": "table", "tableId": "arm_summary"},
            {
                "id": "quality_intro",
                "type": "markdown",
                "body": "## Development quality\nFinal quality is evaluated separately from controller and operator mechanics. A development improvement can satisfy the M2 full-pass rule, but remains susceptible to development overfitting and cannot authorize held-out claims.",
            },
            {"id": "quality_chart_block", "type": "chart", "chartId": "dev_quality"},
            {"id": "timeline_chart_block", "type": "chart", "chartId": "incumbent_timeline"},
            {
                "id": "method",
                "type": "markdown",
                "body": "## Method and validation\nM2-R1A first replayed the frozen 50-generation ledger with zero model/evaluator calls and confirmed the post-GEN-9 escape should move from GEN-41 to GEN-13. M2-R1B then ran two independent 16-generation prospective trajectories with identical model, reasoning effort, development evaluator, and budget. The versioned M2 controller wrote immutable per-generation decisions before the frozen campaign runner executed proposals and gates. An independent validator recomputed timing, root counts, candidate hashes, and development-only evidence-source IDs. All validation checks passed.",
            },
            {
                "id": "limits",
                "type": "markdown",
                "body": "## Causal boundary and limitations\nNo Set Cover blind instance, blind evaluator, or confirmation asset was read. A forced SEED Git root proves a distinct inherited-code root, not an independent semantic algorithm family. Candidate validity is determined by the frozen development evaluator; it does not establish generalization. Each arm is a single causal trajectory, so stochastic uncertainty is not estimated. The frozen four scientific outcomes remain evaluator-owned and are not replaced by this controller-level mechanism label.",
            },
        ],
        "charts": [
            {
                "id": "root_outcomes",
                "title": "Forced root outcomes by prospective arm",
                "subtitle": "The valid-root count separates root creation from development-valid basin jumps.",
                "type": "bar",
                "source": source(
                    "SELECT arm, metric, count FROM root_outcomes ORDER BY arm, metric",
                    "Compare forced and dev-valid new roots across prospective arms.",
                    ["count is the number of preregistered root events in 16 generations"],
                ),
                "dataset": "root_outcomes",
                "encodings": {
                    "x": {"field": "arm", "type": "nominal", "title": "Arm"},
                    "y": {"field": "count", "type": "quantitative", "title": "Roots"},
                    "color": {"field": "metric", "type": "nominal", "title": "Root outcome"},
                },
                "options": {"orientation": "vertical", "grouping": "grouped"},
            },
            {
                "id": "dev_quality",
                "title": "Best Set Cover development speedup",
                "subtitle": "Development quality is reported separately from escape-mechanism success.",
                "type": "bar",
                "source": source(
                    "SELECT series, dev_speedup FROM development_quality ORDER BY dev_speedup DESC",
                    "Compare best prospective development speedups with the frozen EE h50 reference.",
                    ["development speedup is baseline runtime divided by candidate runtime on the frozen development evaluator"],
                ),
                "dataset": "development_quality",
                "encodings": {
                    "x": {"field": "series", "type": "nominal", "title": "Series"},
                    "y": {"field": "dev_speedup", "type": "quantitative", "title": "Development speedup (×)"},
                },
                "options": {"orientation": "vertical"},
            },
            {
                "id": "incumbent_timeline",
                "title": "Development incumbent before each generation",
                "subtitle": "Plateaus drive the strict incumbent-refresh stagnation clock.",
                "type": "line",
                "source": source(
                    "SELECT arm, generation, incumbent_before FROM incumbent_timeline ORDER BY arm, generation",
                    "Show the causal incumbent state used before each prospective generation.",
                    ["incumbent_before is the best development raw_speedup available before proposal generation"],
                ),
                "dataset": "incumbent_timeline",
                "encodings": {
                    "x": {"field": "generation", "type": "quantitative", "title": "Generation"},
                    "y": {"field": "incumbent_before", "type": "quantitative", "title": "Development incumbent (×)"},
                    "color": {"field": "arm", "type": "nominal", "title": "Arm"},
                },
            },
        ],
        "tables": [
            {
                "id": "arm_summary",
                "title": "Prospective arm mechanism and efficiency summary",
                "source": base_source,
                "dataset": "arm_summary",
                "columns": [
                    {"field": "arm", "label": "Arm", "type": "text"},
                    {"field": "timely_escape", "label": "Timely escape", "type": "boolean"},
                    {"field": "new_roots", "label": "New roots", "type": "number"},
                    {"field": "valid_new_roots", "label": "Valid new roots", "type": "number"},
                    {"field": "basin_jump_rate", "label": "Basin-jump rate", "type": "percent"},
                    {"field": "valid_candidates", "label": "Valid candidates", "type": "number"},
                    {"field": "best_dev_speedup", "label": "Best dev speedup", "type": "number"},
                    {"field": "tokens", "label": "Tokens", "type": "number"},
                    {"field": "wall_seconds", "label": "Wall seconds", "type": "number"},
                    {"field": "max_parent_share", "label": "Max parent share", "type": "percent"},
                ],
                "defaultSort": {"field": "best_dev_speedup", "direction": "desc"},
            }
        ],
    }
    snapshot = {
        "version": 1,
        "status": "ready",
        "generatedAt": "2026-08-16T00:00:00Z",
        "datasets": {
            "arm_summary": arm_rows,
            "root_outcomes": root_rows,
            "development_quality": performance_rows,
            "incumbent_timeline": timeline_rows,
            "validation": [
                {
                    "passed": VALIDATION["passed"],
                    "check_count": len(VALIDATION["checks"]),
                    "result_sha256": VALIDATION["result_sha256"],
                }
            ],
        },
    }
    (HERE / "artifact.json").write_text(
        json.dumps({"manifest": manifest, "snapshot": snapshot}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
