"""Build the MCP report manifest and bounded snapshot for the M1 autopsy."""

from __future__ import annotations

import json
import pathlib
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
RESULT = json.loads((HERE / "result.json").read_text())
OUTPUT = HERE / "artifact.json"


def source(query: str, description: str, metrics: list[str]) -> dict[str, Any]:
    return {
        "id": "dev_autopsy",
        "label": "Frozen Set Cover development trajectory autopsy",
        "path": "research/results/algotune_set_cover_mechanism_autopsy_v0/result.json",
        "query": {
            "language": "sql",
            "sql": query,
            "description": description,
            "tables_used": ["result.json"],
            "metric_definitions": metrics,
            "filters": ["development-only evidence", "no blind artifacts"],
        },
    }


CHECKPOINT_SOURCE = source(
    "SELECT arm, horizon, development_raw_speedup, proposal_valid_rate, selected_generation, cumulative_tokens, cumulative_wall_seconds FROM checkpoint_rows ORDER BY horizon, arm",
    "Read frozen checkpoint metrics for the four development trajectories.",
    [
        "development_raw_speedup is the frozen evaluator's raw speedup at a saved horizon",
        "cumulative_tokens and cumulative_wall_seconds are engine-accounted search cost through that horizon",
    ],
)


def arm_label(arm: str) -> str:
    return {
        "ada": "AdaEvolve",
        "evox": "EvoX",
        "shinka": "Shinka",
        "evidence_evolve": "EvidenceEvolve",
    }[arm]


def lineage_rows(arm: str) -> list[dict[str, Any]]:
    rows = []
    for item in RESULT["key_lineages"][f"{arm}_h50"]:
        rows.append(
            {
                "iteration": item["iteration"],
                "candidate": item["candidate_id"][:8],
                "dev_speedup": round(item["raw_speedup"], 6),
                "parent": (item["parent_id"] or "ROOT")[:8],
                "context_count": len(item["context_ids"]),
                "lines_of_code": item["lines_of_code"],
                "change": item["changes"] or "seed/migration record",
            }
        )
    return rows


def main() -> None:
    checkpoint_rows = [
        {
            **row,
            "arm_label": arm_label(row["arm"]),
            "development_raw_speedup": round(row["development_raw_speedup"], 6),
            "cumulative_wall_hours": round(row["cumulative_wall_seconds"] / 3600, 4),
        }
        for row in RESULT["checkpoint_rows"]
    ]

    ee = RESULT["arm_observability"]["evidence_evolve"]
    stage_order = [
        ("generated", "Generated"),
        ("implemented", "Implemented"),
        ("unique_code_artifacts", "Unique code"),
        ("evaluated", "Evaluated"),
        ("development_valid", "Dev-valid"),
        ("incumbent_improvements", "Improved incumbent"),
        ("admitted", "Admitted"),
        ("distinct_candidates_selected_as_parent", "Ever selected parent"),
        ("retained_active", "Final active"),
    ]
    ee_stages = [
        {"stage": label, "count": ee["funnel"][key], "stage_order": position}
        for position, (key, label) in enumerate(stage_order, 1)
    ]
    ee_generations = [
        {
            "generation": row["generation"],
            "raw_speedup": round(row["raw_speedup"], 6) if row["raw_speedup"] is not None else None,
            "valid": row["valid"],
            "improved_incumbent": row["improved_incumbent"],
            "admitted": row["admitted"],
            "selected_as_parent": row["selected_as_parent"],
            "retained_active": row["retained_active"],
            "family": row["family"],
            "mutation_type": row["mutation_type"],
        }
        for row in ee["generation_rows"]
        if row["raw_speedup"] is not None
    ]

    arm_rows = []
    for arm in ("shinka", "ada", "evox"):
        data = RESULT["arm_observability"][arm]
        arm_rows.append(
            {
                "arm": arm_label(arm),
                "generated_records": data["observable_generated_records"],
                "dev_valid_records": data["observable_valid_generated_records"],
                "unique_sources": data["observable_unique_generated_sources"],
                "record_improvements": len(data["record_improvements"]),
                "distinct_parents": data["distinct_selected_parent_ids"],
                "records_with_context": data["generated_records_with_context"],
                "final_store_records": data["final_store_records"],
                "observability_note": "program-store proxy; not a cross-engine admission funnel",
            }
        )
    arm_rows.append(
        {
            "arm": "EvidenceEvolve",
            "generated_records": ee["funnel"]["generated"],
            "dev_valid_records": ee["funnel"]["development_valid"],
            "unique_sources": ee["funnel"]["unique_code_artifacts"],
            "record_improvements": ee["funnel"]["incumbent_improvements"],
            "distinct_parents": ee["funnel"]["distinct_candidates_selected_as_parent"],
            "records_with_context": sum(len(row["parent_ids"]) > 1 for row in ee["generation_rows"]),
            "final_store_records": ee["funnel"]["retained_active"],
            "observability_note": "native proposal/gate/archive ledger",
        }
    )

    hypotheses = RESULT["mechanism_hypotheses"]
    query_base = "SELECT * FROM result_json"
    dev_source = source(
        query_base,
        "Read the complete frozen development-only autopsy result.",
        [
            "incumbent improvement means a dev-valid candidate strictly exceeded every earlier development speedup",
            "selected as parent means a candidate ID appears in a later proposal's parent_ids",
            "retained active means active=1 in the final EvidenceEvolve island_memberships state",
        ],
    )

    manifest = {
        "version": 1,
        "title": "Set Cover Search Mechanism Autopsy",
        "surface": "report",
        "description": "Development-only diagnosis of compute-to-algorithm-improvement dynamics across four search engines.",
        "generatedAt": "2026-08-16T00:00:00Z",
        "sources": [dev_source],
        "blocks": [
            {
                "id": "title",
                "type": "markdown",
                "body": "# Set Cover Search Mechanism Autopsy\nM1 development-only diagnosis. The blind campaign remains frozen; this report did not read blind instances, per-case failures, or blind evaluator logs.",
            },
            {
                "id": "technical_summary",
                "type": "markdown",
                "sourceId": "dev_autopsy",
                "body": "## Technical summary\n**Primary diagnosis:** EvidenceEvolve failed at the conversion from valid search activity to cumulative incumbent improvement. It generated 50 proposals, produced 45 distinct evaluated code artifacts, and admitted all 36 dev-valid candidates, yet refreshed the incumbent only twice (GEN-7 and GEN-9). After GEN-9, 41 more proposals, 36 evaluations, and 33 valid candidates produced **zero** incumbent improvements. This is not a proposal-volume failure. It is a combined admission/parent-pressure failure plus a narrow local-search collapse.\n\nAda is the positive control: its h50 result sits on a 14-record ancestry with cross-lineage context at nearly every substantive step, and late discoveries are recombined into the final candidate. EvoX exhibits a different mechanism: an explicitly radical iteration-28 mutation switches from MiniCard bound tightening to RC2/MaxSAT and jumps from 2.389× to 9.910× on development.",
            },
            {"id": "checkpoint_findings", "type": "markdown", "body": "## Key findings with visual evidence\nThe checkpoint curve separates three useful dynamics: Shinka improves early then flattens; Ada compounds improvements across the horizon; EvoX remains weak until a late basin jump; EvidenceEvolve reaches its final incumbent by GEN-9 and never advances again."},
            {"id": "checkpoint_curve_block", "type": "chart", "chartId": "checkpoint_curve"},
            {"id": "ee_funnel_text", "type": "markdown", "sourceId": "dev_autopsy", "body": "## EvidenceEvolve conversion anatomy\nThe counts below are observable stage/cohort counts, not a mathematically nested funnel: `selected as parent` and `final active` are later-state properties. The decisive mismatch is 36 dev-valid candidates → 2 incumbent improvements → 36 admissions. Admission was baseline-relative (`POSITIVE_HEADROOM`), not incumbent-relative, so candidates below GEN-9 still became `ELITE`/`CODE_PARENT`. GEN-13 (1.0116×) was then referenced as a parent 36 times despite trailing the GEN-9 incumbent (1.0272×)."},
            {"id": "ee_funnel_block", "type": "chart", "chartId": "ee_stage_counts"},
            {"id": "ee_generation_block", "type": "chart", "chartId": "ee_candidate_quality"},
            {"id": "four_arm_decomposition", "type": "markdown", "body": "## Four-arm checkpoint decomposition\nOnly EvidenceEvolve exposes a native generated→gate→archive ledger. Ada/EvoX checkpoint stores and the Shinka database expose program-retention proxies with different semantics, so their counts are shown as observability measures rather than falsely harmonized admissions."},
            {"id": "arm_table_block", "type": "table", "tableId": "arm_decomposition"},
            {"id": "ada_positive_control", "type": "markdown", "sourceId": "dev_autopsy", "body": "## Ada positive control: h12 → h24 → h50\nAda accumulates algorithmic structure: reusable exact cardinality, greedy bounds, safe forced/dominated-set reductions, bit encoding/kernelization, and finally cheap certification of very small covers before MiniCard. Its ancestry also demonstrates explicit recombination. The h45 6.884× branch is supplied as context to h48; h49 reaches 7.046×; h49 is then context for h50, which reaches 7.052×. The final program grows from 51 to 167 lines, indicating cumulative composition rather than repeated one-line controls."},
            {"id": "ada_lineage_block", "type": "table", "tableId": "ada_lineage"},
            {"id": "evox_breakthrough", "type": "markdown", "sourceId": "dev_autopsy", "body": "## EvoX h50 breakthrough lineage\nIteration 28 is the causal discontinuity visible in the frozen search record. A `FUNDAMENTALLY DIFFERENT APPROACH` operator acts on a 2.389× MiniCard lineage with no context donor and replaces iterative cardinality tightening with unit-cost RC2/MaxSAT, producing 9.910×. Later work adds deduplication, dominance pruning, and forced-set reduction, yielding 13.436× at iteration 40 and 13.482× at h50. This supports a protected long-horizon radical-mutation quota.\n\nThe h24 checkpoint was admitted because its iteration-20 candidate passed all 100 fixed development instances (`valid_rate=1.0`) and was the dev incumbent at 2.409×. The frozen mechanism had no independent generalization gate at checkpoint selection. From development evidence alone, it was not known to be incomplete; therefore the mechanism-level issue is reliance on a single fixed dev validity surface, not a knowingly bypassed correctness failure. No blind cases were inspected to reach this conclusion."},
            {"id": "evox_lineage_block", "type": "table", "tableId": "evox_lineage"},
            {"id": "scope_definitions", "type": "markdown", "body": "## Scope, data, and metric definitions\n**Allowed evidence:** frozen trajectories, candidate source/diffs, development evaluator metrics, prompts/responses, parent/context lineage, archive/selection state, gate outcomes, and token/wall metadata under `runs/algotune_horizon_scaling_v0/set_cover`. **Excluded evidence:** every blind instance, per-case blind failure, and blind evaluator log.\n\n`Improved incumbent` is strict improvement over all prior dev-valid candidates. `Admitted` is EvidenceEvolve population admission. `Selected as parent` means later appearance in `parent_ids`. `Retained active` is the final active island membership. Upstream program-store counts are not treated as equivalent to EvidenceEvolve admissions."},
            {"id": "methodology", "type": "markdown", "body": "## Methodology\nA deterministic parser reconstructs the four checkpoint curves, deduplicates SkyDiscover program records across checkpoints, follows direct parent ancestry, counts context donors, reads Shinka's native SQLite ledger, and joins EvidenceEvolve proposal, implementation, receipt, population, and island tables. Incumbent improvements are recomputed chronologically from frozen development speedups. Candidate source hashes and line counts are retained for lineage auditability."},
            {"id": "limitations", "type": "markdown", "body": "## Limitations and robustness\nThis is a single-task mechanism autopsy, so engine-level scaling laws remain hypotheses until replicated on other tasks. Wall and token accounting differ by engine and are useful for within-engine marginal return, not exact cross-engine cost equivalence. Ada's checkpoint store prunes or migrates records, while EvoX retains all records and Shinka has its own archive semantics; cross-engine funnel stages are therefore intentionally not inferred. Development speed differences near 1.0272× are small, but the central EE conclusion is robust because no post-GEN-9 candidate exceeds the incumbent across 41 further generations."},
            {"id": "hypotheses", "type": "markdown", "sourceId": "dev_autopsy", "body": "## Mechanism hypotheses\n**H1 — strongly supported:** admission pressure is misaligned with incumbent improvement. **H2 — strongly supported:** cumulative innovation collapses into a narrow control lineage (38 of 41 post-GEN-9 proposals are controls, simplifications, or failure-directed variants; GEN-13 is reused 36 times). **H3 — supported:** Ada scales through ancestry plus cross-lineage composition. **H4 — strongly supported:** EvoX's late scaling depends on a protected radical-mutation escape path. Each hypothesis has a falsifiable intervention prediction in the source result."},
            {"id": "next_steps", "type": "markdown", "body": "## Next steps\nDo not modify the frozen campaign or run more Set Cover blind work. First implement offline counterfactual replays of selection only: incumbent-relative parent scoring, persistent-family diversity, and explicit restart/radical quotas using the already generated development ledger. Then define an EvidenceEvolve portfolio controller: Shinka/Ada receive early budget, arms are down-weighted on sustained marginal-return collapse, and EvoX keeps a separately protected long-horizon quota. Any later EE code change should map to one hypothesis and be evaluated on a new development campaign before any fresh confirmation asset is authorized."},
            {"id": "questions", "type": "markdown", "body": "## Further questions\n1. Does GEN-13 dominance persist under an incumbent-relative counterfactual parent selector? 2. How many persistent algorithm families remain after canonicalizing source changes into mechanism families rather than code hashes? 3. Can Ada's context composition be ablated without changing evaluator semantics? 4. What minimum protected radical quota preserves EvoX's iteration-28-style escapes without overwhelming correctness gates? 5. Do the same early/mid/late/stagnant dynamics replicate on Graph Coloring or another untouched task?"},
        ],
        "charts": [
            {
                "id": "checkpoint_curve",
                "title": "Development speedup by search horizon",
                "subtitle": "EE stops improving after GEN-9; Ada compounds and EvoX jumps late.",
                "type": "line",
                "source": CHECKPOINT_SOURCE,
                "dataset": "checkpoint_rows",
                "encodings": {
                    "x": {"field": "horizon", "type": "quantitative", "title": "Horizon"},
                    "y": {"field": "development_raw_speedup", "type": "quantitative", "title": "Development speedup (×)"},
                    "color": {"field": "arm_label", "type": "nominal", "title": "Search engine"},
                },
            },
            {
                "id": "ee_stage_counts",
                "title": "EvidenceEvolve observable stage counts",
                "subtitle": "36 dev-valid candidates were admitted, but only two ever improved the incumbent.",
                "type": "bar",
                "source": source(
                    "SELECT stage, count, stage_order FROM arm_observability_evidence_evolve_funnel ORDER BY stage_order",
                    "Read EvidenceEvolve proposal, evaluator, gate, parent, and final-active counts.",
                    ["Counts are stage/cohort observations and are not all strict subsets"],
                ),
                "dataset": "ee_stages",
                "encodings": {
                    "x": {"field": "stage", "type": "nominal", "title": "Observed stage"},
                    "y": {"field": "count", "type": "quantitative", "title": "Candidates"},
                },
                "options": {"orientation": "vertical"},
            },
            {
                "id": "ee_candidate_quality",
                "title": "EvidenceEvolve evaluated candidate quality by generation",
                "subtitle": "Valid proposal activity continues, but no candidate after GEN-9 exceeds 1.0272×.",
                "type": "scatter",
                "source": source(
                    "SELECT generation, raw_speedup, valid, improved_incumbent, admitted, selected_as_parent, retained_active, family, mutation_type FROM evidence_evolve_generation_rows WHERE raw_speedup IS NOT NULL ORDER BY generation",
                    "Read evaluated EvidenceEvolve candidates and their downstream search states.",
                    ["raw_speedup is development-only", "improved_incumbent is a strict chronological record"],
                ),
                "dataset": "ee_generations",
                "encodings": {
                    "x": {"field": "generation", "type": "quantitative", "title": "Generation"},
                    "y": {"field": "raw_speedup", "type": "quantitative", "title": "Development speedup (×)"},
                    "color": {"field": "mutation_type", "type": "nominal", "title": "Mutation type"},
                },
            },
        ],
        "tables": [
            {
                "id": "arm_decomposition",
                "title": "Observable search decomposition by engine",
                "source": dev_source,
                "dataset": "arm_rows",
                "columns": [
                    {"field": "arm", "label": "Engine", "type": "text"},
                    {"field": "generated_records", "label": "Generated", "type": "number"},
                    {"field": "dev_valid_records", "label": "Dev-valid", "type": "number"},
                    {"field": "unique_sources", "label": "Unique source", "type": "number"},
                    {"field": "record_improvements", "label": "Record improvements", "type": "number"},
                    {"field": "distinct_parents", "label": "Distinct parents", "type": "number"},
                    {"field": "records_with_context", "label": "With context", "type": "number"},
                    {"field": "final_store_records", "label": "Final store", "type": "number"},
                    {"field": "observability_note", "label": "Semantics", "type": "text"},
                ],
                "defaultSort": {"field": "record_improvements", "direction": "desc"},
            },
            {
                "id": "ada_lineage",
                "title": "AdaEvolve h50 direct ancestry",
                "source": dev_source,
                "dataset": "ada_lineage",
                "columns": [
                    {"field": "iteration", "label": "Iteration", "type": "number"},
                    {"field": "candidate", "label": "Candidate", "type": "text"},
                    {"field": "dev_speedup", "label": "Dev speedup", "type": "number"},
                    {"field": "parent", "label": "Parent", "type": "text"},
                    {"field": "context_count", "label": "Contexts", "type": "number"},
                    {"field": "lines_of_code", "label": "LOC", "type": "number"},
                    {"field": "change", "label": "Recorded change", "type": "text"},
                ],
                "defaultSort": {"field": "iteration", "direction": "asc"},
            },
            {
                "id": "evox_lineage",
                "title": "EvoX h50 direct ancestry",
                "source": dev_source,
                "dataset": "evox_lineage",
                "columns": [
                    {"field": "iteration", "label": "Iteration", "type": "number"},
                    {"field": "candidate", "label": "Candidate", "type": "text"},
                    {"field": "dev_speedup", "label": "Dev speedup", "type": "number"},
                    {"field": "parent", "label": "Parent", "type": "text"},
                    {"field": "context_count", "label": "Contexts", "type": "number"},
                    {"field": "lines_of_code", "label": "LOC", "type": "number"},
                    {"field": "change", "label": "Recorded change", "type": "text"},
                ],
                "defaultSort": {"field": "iteration", "direction": "asc"},
            },
        ],
    }

    snapshot = {
        "version": 1,
        "status": "ready",
        "generatedAt": "2026-08-16T00:00:00Z",
        "datasets": {
            "checkpoint_rows": checkpoint_rows,
            "ee_stages": ee_stages,
            "ee_generations": ee_generations,
            "arm_rows": arm_rows,
            "ada_lineage": lineage_rows("ada"),
            "evox_lineage": lineage_rows("evox"),
            "hypotheses": hypotheses,
        },
    }
    OUTPUT.write_text(json.dumps({"manifest": manifest, "snapshot": snapshot}, indent=2) + "\n")


if __name__ == "__main__":
    main()
