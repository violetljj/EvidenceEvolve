"""Development-only Set Cover search-mechanism autopsy.

This script intentionally reads only the frozen horizon-scaling development run.
It does not import, enumerate, or inspect any blind-evaluation artifact.
"""

from __future__ import annotations

import collections
import glob
import hashlib
import json
import pathlib
import sqlite3
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[3]
RUN = ROOT / "runs" / "algotune_horizon_scaling_v0" / "set_cover" / "arms"
OUT = pathlib.Path(__file__).with_name("result.json")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def speed(record: dict[str, Any]) -> float:
    metrics = record.get("metrics") or {}
    return float(metrics.get("raw_speedup", metrics.get("combined_score", 0.0)) or 0.0)


def checkpoint_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for arm in ("shinka", "ada", "evox", "evidence_evolve"):
        trajectory = load_json(RUN / arm / "trajectory_result.json")
        for cp in trajectory["checkpoints"]:
            rows.append(
                {
                    "arm": arm,
                    "horizon": cp["horizon"],
                    "development_raw_speedup": cp["development_raw_speedup"],
                    "proposal_valid_rate": cp["proposal_valid_rate"],
                    "selected_generation": cp["selected_generation"],
                    "selected_id": cp["selected_id"],
                    "candidate_sha256": cp["candidate_sha256"],
                    "cumulative_tokens": cp["cumulative_tokens"],
                    "cumulative_wall_seconds": cp["cumulative_wall_seconds"],
                }
            )
    return rows


def sky_programs(arm: str) -> dict[str, dict[str, Any]]:
    programs: dict[str, dict[str, Any]] = {}
    pattern = RUN / arm / "upstream" / "checkpoints" / "checkpoint_*" / "programs" / "*.json"
    for name in glob.glob(str(pattern)):
        record = load_json(pathlib.Path(name))
        programs[record["id"]] = record
    return programs


def record_improvements(programs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    best = max((speed(p) for p in programs.values() if p.get("iteration_found") == 0), default=0.0)
    rows: list[dict[str, Any]] = []
    by_iteration: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    for program in programs.values():
        by_iteration[int(program.get("iteration_found", 0))].append(program)
    for iteration in sorted(by_iteration):
        if iteration == 0:
            continue
        candidate = max(by_iteration[iteration], key=speed)
        candidate_speed = speed(candidate)
        if candidate_speed > best:
            rows.append(
                {
                    "iteration": iteration,
                    "candidate_id": candidate["id"],
                    "raw_speedup": candidate_speed,
                    "parent_id": candidate.get("parent_id"),
                    "context_ids": candidate.get("other_context_ids") or [],
                    "changes": (candidate.get("metadata") or {}).get("changes", ""),
                    "lines_of_code": len((candidate.get("solution") or "").splitlines()),
                }
            )
            best = candidate_speed
    return rows


def ancestry(programs: dict[str, dict[str, Any]], candidate_id: str) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = programs.get(candidate_id)
    while current and current["id"] not in seen:
        seen.add(current["id"])
        chain.append(
            {
                "candidate_id": current["id"],
                "iteration": current.get("iteration_found", 0),
                "raw_speedup": speed(current),
                "parent_id": current.get("parent_id"),
                "context_ids": current.get("other_context_ids") or [],
                "changes": (current.get("metadata") or {}).get("changes", ""),
                "lines_of_code": len((current.get("solution") or "").splitlines()),
                "source_sha256": hashlib.sha256((current.get("solution") or "").encode()).hexdigest(),
            }
        )
        current = programs.get(current.get("parent_id"))
    chain.reverse()
    return chain


def sky_summary(arm: str) -> dict[str, Any]:
    programs = sky_programs(arm)
    roots = [p for p in programs.values() if int(p.get("iteration_found", 0)) == 0]
    generated = [p for p in programs.values() if int(p.get("iteration_found", 0)) > 0]
    selected_parents = {p.get("parent_id") for p in generated if p.get("parent_id")}
    context_programs = [p for p in generated if p.get("other_context_ids")]
    call_files = [
        path
        for path in (RUN / arm / "calls").glob("*.json")
        if not path.name.endswith(".schema.json")
    ]
    final_checkpoint = load_json(RUN / arm / "trajectory_result.json")["checkpoints"][-1]
    return {
        "proposal_calls": len(call_files),
        "observable_program_records": len(programs),
        "observable_generated_records": len(generated),
        "observable_valid_generated_records": sum(bool(p.get("metrics", {}).get("correct")) for p in generated),
        "observable_unique_generated_sources": len({p.get("solution") or "" for p in generated}),
        "distinct_selected_parent_ids": len(selected_parents),
        "generated_records_with_context": len(context_programs),
        "record_improvements": record_improvements(programs),
        "final_store_records": len(list((RUN / arm / "upstream" / "checkpoints" / "checkpoint_50" / "programs").glob("*.json"))),
        "final_candidate_id": final_checkpoint["selected_id"],
        "final_ancestry": ancestry(programs, final_checkpoint["selected_id"]),
        "root_records": len(roots),
    }


def shinka_summary() -> dict[str, Any]:
    connection = sqlite3.connect(RUN / "shinka" / "upstream" / "programs.sqlite")
    connection.row_factory = sqlite3.Row
    programs = [dict(row) for row in connection.execute("select * from programs order by generation, timestamp")]
    generated = [p for p in programs if p["generation"] > 0]
    baseline = max(p["combined_score"] or 0.0 for p in programs if p["generation"] == 0)
    best = baseline
    improvements: list[dict[str, Any]] = []
    for generation in sorted({p["generation"] for p in generated}):
        candidate = max((p for p in generated if p["generation"] == generation), key=lambda p: p["combined_score"] or 0.0)
        candidate_speed = float(candidate["combined_score"] or 0.0)
        if candidate_speed > best:
            metadata = json.loads(candidate["metadata"] or "{}")
            improvements.append(
                {
                    "iteration": generation,
                    "candidate_id": candidate["id"],
                    "raw_speedup": candidate_speed,
                    "parent_id": candidate["parent_id"],
                    "context_ids": json.loads(candidate["archive_inspiration_ids"] or "[]")
                    + json.loads(candidate["top_k_inspiration_ids"] or "[]"),
                    "changes": metadata.get("patch_description", ""),
                    "lines_of_code": len(candidate["code"].splitlines()),
                }
            )
            best = candidate_speed
    return {
        "proposal_calls": len(generated),
        "observable_program_records": len(programs),
        "observable_generated_records": len(generated),
        "observable_valid_generated_records": sum(bool(p["correct"]) for p in generated),
        "observable_unique_generated_sources": len({p["code"] for p in generated}),
        "distinct_selected_parent_ids": len({p["parent_id"] for p in generated if p["parent_id"]}),
        "generated_records_with_context": sum(
            bool(json.loads(p["archive_inspiration_ids"] or "[]") or json.loads(p["top_k_inspiration_ids"] or "[]"))
            for p in generated
        ),
        "record_improvements": improvements,
        "final_store_records": connection.execute("select count(*) from archive").fetchone()[0],
    }


def receipt_speed(path: pathlib.Path) -> float | None:
    data = load_json(path)

    def find(value: Any) -> float | None:
        if isinstance(value, dict):
            if isinstance(value.get("raw_speedup"), (int, float)):
                return float(value["raw_speedup"])
            for child in value.values():
                found = find(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find(child)
                if found is not None:
                    return found
        return None

    return find(data)


def evidence_evolve_summary() -> dict[str, Any]:
    campaign = RUN / "evidence_evolve" / "campaign"
    proposals: dict[str, dict[str, Any]] = {}
    for path in campaign.glob("generations/GEN-*/proposals/*.json"):
        candidate = load_json(path)["acquisition"]["candidate"]
        proposals[candidate["candidate_id"]] = candidate

    connection = sqlite3.connect(campaign / "research.db")
    connection.row_factory = sqlite3.Row
    candidate_rows = {row["candidate_id"]: dict(row) for row in connection.execute("select * from candidates")}
    population = {row["candidate_id"]: dict(row) for row in connection.execute("select * from population_candidates")}
    active = {
        row["candidate_id"]
        for row in connection.execute("select candidate_id from island_memberships where active = 1")
    }
    receipt_values: dict[str, float | None] = {}
    for candidate_id, row in candidate_rows.items():
        receipt_values[candidate_id] = receipt_speed(campaign / row["receipt_path"])

    implemented = 0
    implementation_status = collections.Counter()
    code_hashes: set[str] = set()
    for path in campaign.glob("candidates/GEN-*/implementation.json"):
        implementation = load_json(path)
        status = implementation.get("status", "UNKNOWN")
        implementation_status[status] += 1
        if status == "IMPLEMENTED":
            implemented += 1
    for row in connection.execute("select code_sha256 from code_artifacts"):
        code_hashes.add(row["code_sha256"])

    baseline = 0.9987262059725405
    incumbent = baseline
    improvements: list[dict[str, Any]] = []
    generation_rows: list[dict[str, Any]] = []
    for candidate_id in sorted(proposals):
        value = receipt_values.get(candidate_id)
        valid = candidate_rows.get(candidate_id, {}).get("scientific_outcome") == "POSITIVE_HEADROOM"
        improved = bool(valid and value is not None and value > incumbent)
        if improved:
            incumbent = float(value)
            improvements.append({"candidate_id": candidate_id, "raw_speedup": value})
        generation_rows.append(
            {
                "candidate_id": candidate_id,
                "generation": int(candidate_id[4:7]),
                "raw_speedup": value,
                "valid": valid,
                "improved_incumbent": improved,
                "admitted": candidate_id in population,
                "selected_as_parent": False,
                "retained_active": candidate_id in active,
                "family": proposals[candidate_id]["family"],
                "mutation_type": proposals[candidate_id]["mutation_type"],
                "parent_ids": proposals[candidate_id]["parent_ids"],
            }
        )
    parent_counts = collections.Counter(
        parent
        for candidate in proposals.values()
        for parent in candidate["parent_ids"]
        if parent != "SEED"
    )
    for row in generation_rows:
        row["selected_as_parent"] = row["candidate_id"] in parent_counts

    post_incumbent = [row for row in generation_rows if row["generation"] > 9]
    mutation_counts = collections.Counter(row["mutation_type"] for row in generation_rows)
    return {
        "baseline_raw_speedup": baseline,
        "implementation_status": dict(sorted(implementation_status.items())),
        "funnel": {
            "generated": len(proposals),
            "implemented": implemented,
            "unique_code_artifacts": len(code_hashes),
            "evaluated": len(candidate_rows),
            "development_valid": len(population),
            "incumbent_improvements": len(improvements),
            "admitted": len(population),
            "distinct_candidates_selected_as_parent": len(parent_counts),
            "retained_active": len(active),
        },
        "improvements": improvements,
        "parent_selection_counts": dict(parent_counts.most_common()),
        "active_candidates": sorted(active),
        "generation_rows": generation_rows,
        "post_gen9": {
            "generations": len(post_incumbent),
            "evaluated": sum(row["raw_speedup"] is not None for row in post_incumbent),
            "valid": sum(row["valid"] for row in post_incumbent),
            "incumbent_improvements": sum(row["improved_incumbent"] for row in post_incumbent),
            "control_or_simplification": sum(
                row["mutation_type"] in {"control_mutation", "simplification", "failure_directed_mutation"}
                for row in post_incumbent
            ),
        },
        "mutation_type_counts": dict(mutation_counts),
    }


def main() -> None:
    ada = sky_summary("ada")
    evox = sky_summary("evox")
    shinka = shinka_summary()
    evidence_evolve = evidence_evolve_summary()
    result = {
        "schema_version": "1.0",
        "scope": "frozen Set Cover development trajectories only",
        "blind_artifacts_read": False,
        "checkpoint_rows": checkpoint_rows(),
        "arm_observability": {
            "shinka": shinka,
            "ada": ada,
            "evox": evox,
            "evidence_evolve": evidence_evolve,
        },
        "key_lineages": {
            "ada_h50": ada["final_ancestry"],
            "evox_h50": evox["final_ancestry"],
        },
        "mechanism_hypotheses": [
            {
                "id": "H1",
                "label": "EE admission pressure is misaligned with incumbent improvement",
                "status": "strongly_supported",
                "testable_prediction": "An incumbent-relative admission/parent score would sharply reduce CODE_PARENT promotions after GEN-9.",
            },
            {
                "id": "H2",
                "label": "EE cumulative innovation collapses into a narrow control lineage",
                "status": "strongly_supported",
                "testable_prediction": "Mechanism-family quotas or explicit restart branches would increase distinct persistent parent chains and incumbent refreshes.",
            },
            {
                "id": "H3",
                "label": "Ada scales through ancestry plus cross-lineage context composition",
                "status": "supported",
                "testable_prediction": "Removing context/recombination should reduce late integration of h45/h49 discoveries into the h50 lineage.",
            },
            {
                "id": "H4",
                "label": "EvoX late scaling depends on a protected radical-mutation escape path",
                "status": "strongly_supported",
                "testable_prediction": "Removing the fundamentally-different-approach operator would eliminate or delay the iteration-28 MiniCard-to-RC2 basin jump.",
            },
        ],
    }
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
