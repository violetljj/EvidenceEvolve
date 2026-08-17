from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from evidence_evolve.hashing import sha256_bytes


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _public_average(row: dict[str, Any]) -> float | None:
    values = list(row["public_metrics"].values())
    if not values:
        return None
    try:
        return sum(float(value) for value in values) / len(values)
    except (TypeError, ValueError):
        return None


def _is_better(candidate: dict[str, Any], incumbent: dict[str, Any]) -> bool:
    candidate_score = candidate["combined_score"]
    incumbent_score = incumbent["combined_score"]
    if candidate_score is not None and incumbent_score is not None:
        if candidate_score != incumbent_score:
            return bool(candidate_score > incumbent_score)
    elif candidate_score is not None:
        return True
    elif incumbent_score is not None:
        return False
    candidate_average = _public_average(candidate)
    incumbent_average = _public_average(incumbent)
    if candidate_average is not None and incumbent_average is not None:
        if candidate_average != incumbent_average:
            return candidate_average > incumbent_average
    return float(candidate["timestamp"]) > float(incumbent["timestamp"])


def audit_shinka_selection(
    database: Path,
    *,
    imported_best_program_id: str,
    selected_candidate: Path,
) -> dict[str, Any]:
    """Audit native Shinka fitness mapping and retained-best behavior read-only."""
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = []
        for raw in connection.execute(
            "SELECT id, code, generation, timestamp, combined_score, public_metrics, "
            "correct FROM programs ORDER BY timestamp, generation, id"
        ):
            row = dict(raw)
            row["correct"] = bool(row["correct"])
            row["public_metrics"] = _json_object(row["public_metrics"])
            rows.append(row)
        metadata = {
            str(row["key"]): row["value"]
            for row in connection.execute("SELECT key, value FROM metadata_store")
        }
    finally:
        connection.close()
    if not rows:
        raise ValueError("Shinka selection audit requires at least one program")

    valid_mapping_failures: list[str] = []
    invalid_mapping_failures: list[str] = []
    order_violations: list[dict[str, str]] = []
    history_best: dict[str, Any] | None = None
    trajectory = []
    prior_peak = float("-inf")
    for row in rows:
        raw_speedup = row["public_metrics"].get("raw_speedup")
        raw_value = float(raw_speedup) if raw_speedup is not None else None
        combined = float(row["combined_score"] or 0.0)
        if row["correct"]:
            if raw_value is None or combined != raw_value:
                valid_mapping_failures.append(str(row["id"]))
        elif combined != 0.0:
            invalid_mapping_failures.append(str(row["id"]))

        promoted = False
        if row["correct"] and (history_best is None or _is_better(row, history_best)):
            history_best = row
            promoted = True
        better_but_not_promoted = bool(
            row["correct"]
            and raw_value is not None
            and raw_value > prior_peak
            and not promoted
        )
        if row["correct"] and raw_value is not None:
            prior_peak = max(prior_peak, raw_value)
        trajectory.append(
            {
                "generation": int(row["generation"]),
                "candidate_id": str(row["id"]),
                "candidate_valid": bool(row["correct"]),
                "raw_speedup": raw_value,
                "combined_score": combined,
                "promoted_candidate_id": str(row["id"]) if promoted else None,
                "current_historical_best_candidate_id": (
                    str(history_best["id"]) if history_best else None
                ),
                "better_candidate_generated_but_not_promoted": better_but_not_promoted,
            }
        )

    valid_rows = [row for row in rows if row["correct"]]
    for candidate in valid_rows:
        candidate_raw = candidate["public_metrics"].get("raw_speedup")
        if candidate_raw is None:
            continue
        for comparator in valid_rows:
            comparator_raw = comparator["public_metrics"].get("raw_speedup")
            if comparator_raw is None:
                continue
            if float(candidate_raw) > float(comparator_raw) and float(
                candidate["combined_score"] or 0.0
            ) < float(comparator["combined_score"] or 0.0):
                order_violations.append(
                    {"higher_raw_candidate_id": str(candidate["id"]), "lower_raw_candidate_id": str(comparator["id"])}
                )

    formal_best_id = str(metadata.get("best_program_id") or "")
    simulated_best_id = str(history_best["id"]) if history_best else ""
    selected_sha256 = sha256_bytes(
        selected_candidate.read_bytes().replace(b"\r\n", b"\n")
    )
    imported_row = next(
        (row for row in rows if str(row["id"]) == imported_best_program_id), None
    )
    imported_code_sha256 = (
        sha256_bytes(
            str(imported_row["code"]).encode("utf-8").replace(b"\r\n", b"\n")
        )
        if imported_row
        else None
    )
    valid_scores = [float(row["combined_score"] or 0.0) for row in valid_rows]
    final_score_is_valid_maximum = bool(
        imported_row
        and imported_row["correct"]
        and valid_scores
        and float(imported_row["combined_score"] or 0.0) == max(valid_scores)
    )
    gates = {
        "valid_combined_score_equals_raw_speedup": not valid_mapping_failures,
        "invalid_combined_score_equals_zero": not invalid_mapping_failures,
        "raw_speedup_order_never_inverts_fitness": not order_violations,
        "native_best_matches_simulated_formal_rule": formal_best_id == simulated_best_id,
        "imported_best_matches_native_best": imported_best_program_id == formal_best_id,
        "selected_candidate_matches_imported_best": imported_code_sha256 == selected_sha256,
        "final_return_is_valid_fitness_maximum": final_score_is_valid_maximum,
        "no_better_candidate_generated_but_not_promoted": not any(
            row["better_candidate_generated_but_not_promoted"] for row in trajectory
        ),
    }
    return {
        "schema_version": "1.0",
        "status": "PASS" if all(gates.values()) else "FAIL",
        "database": str(database.resolve()),
        "program_count": len(rows),
        "valid_program_count": len(valid_rows),
        "trajectory": trajectory,
        "formal_best_program_id": formal_best_id,
        "simulated_best_program_id": simulated_best_id,
        "imported_best_program_id": imported_best_program_id,
        "valid_mapping_failure_ids": valid_mapping_failures,
        "invalid_mapping_failure_ids": invalid_mapping_failures,
        "fitness_order_violations": order_violations,
        "gates": gates,
    }
