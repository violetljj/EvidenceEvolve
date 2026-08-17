from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evidence_evolve.benchmarks.shinka_selection_audit import audit_shinka_selection


def _database(path: Path, *, broken_mapping: bool = False) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "CREATE TABLE programs (id TEXT PRIMARY KEY, code TEXT, generation INTEGER, "
            "timestamp REAL, combined_score REAL, public_metrics TEXT, correct BOOLEAN);"
            "CREATE TABLE metadata_store (key TEXT PRIMARY KEY, value TEXT);"
        )
        rows = [
            ("seed", "seed", 0, 1.0, 1.0, {"raw_speedup": 1.0, "valid_rate": 1.0}, 1),
            ("fast", "fast", 1, 2.0, 2.5 if not broken_mapping else 0.0, {"raw_speedup": 2.5, "valid_rate": 1.0}, 1),
            ("invalid", "invalid", 2, 3.0, 0.0, {"raw_speedup": 99.0, "valid_rate": 0.0}, 0),
        ]
        connection.executemany(
            "INSERT INTO programs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(a, b, c, d, e, json.dumps(f), g) for a, b, c, d, e, f, g in rows],
        )
        connection.execute("INSERT INTO metadata_store VALUES ('best_program_id', 'fast')")
        connection.commit()
    finally:
        connection.close()


def test_selection_audit_passes_mapped_valid_best(tmp_path: Path) -> None:
    database = tmp_path / "programs.sqlite"
    selected = tmp_path / "selected.py"
    selected.write_text("fast", encoding="utf-8")
    _database(database)

    audit = audit_shinka_selection(
        database, imported_best_program_id="fast", selected_candidate=selected
    )

    assert audit["status"] == "PASS"
    assert audit["formal_best_program_id"] == "fast"
    assert audit["trajectory"][1]["promoted_candidate_id"] == "fast"
    assert all(audit["gates"].values())


def test_selection_audit_fails_zeroed_valid_speedup(tmp_path: Path) -> None:
    database = tmp_path / "programs.sqlite"
    selected = tmp_path / "selected.py"
    selected.write_text("fast", encoding="utf-8")
    _database(database, broken_mapping=True)

    audit = audit_shinka_selection(
        database, imported_best_program_id="fast", selected_candidate=selected
    )

    assert audit["status"] == "FAIL"
    assert audit["valid_mapping_failure_ids"] == ["fast"]
    assert audit["gates"]["valid_combined_score_equals_raw_speedup"] is False
