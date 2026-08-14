from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evidence_evolve.models import CandidateGenome, ReceiptEnvelope


class ArchiveStore:
    def __init__(self, database: Path):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    parent_ids_json TEXT NOT NULL,
                    island TEXT NOT NULL,
                    family TEXT NOT NULL,
                    mutation_type TEXT NOT NULL,
                    archive_class TEXT NOT NULL,
                    gate_decision TEXT NOT NULL,
                    scientific_outcome TEXT NOT NULL,
                    receipt_path TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_archive
                    ON candidates(archive_class, island);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def record(
        self,
        candidate: CandidateGenome,
        envelope: ReceiptEnvelope,
        receipt_path: Path,
    ) -> None:
        verdict = envelope.receipt.verdict
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, parent_ids_json, island, family, mutation_type,
                    archive_class, gate_decision, scientific_outcome, receipt_path,
                    receipt_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    archive_class=excluded.archive_class,
                    gate_decision=excluded.gate_decision,
                    scientific_outcome=excluded.scientific_outcome,
                    receipt_path=excluded.receipt_path,
                    receipt_sha256=excluded.receipt_sha256,
                    created_at_utc=excluded.created_at_utc
                """,
                (
                    candidate.candidate_id,
                    json.dumps(candidate.parent_ids, sort_keys=True),
                    candidate.island,
                    candidate.family,
                    candidate.mutation_type.value,
                    verdict.archive_class.value,
                    verdict.decision.value,
                    verdict.scientific_outcome.value,
                    receipt_path.as_posix(),
                    envelope.receipt_sha256,
                    envelope.receipt.created_at_utc,
                ),
            )

    def summary(self) -> dict[str, object]:
        with self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            by_class = dict(
                connection.execute(
                    "SELECT archive_class, COUNT(*) FROM candidates "
                    "GROUP BY archive_class ORDER BY archive_class"
                ).fetchall()
            )
            rows = connection.execute(
                "SELECT candidate_id, parent_ids_json, archive_class, gate_decision "
                "FROM candidates ORDER BY created_at_utc, candidate_id"
            ).fetchall()
        return {
            "total": total,
            "by_archive_class": by_class,
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "parent_ids": json.loads(parent_ids),
                    "archive_class": archive_class,
                    "gate_decision": decision,
                }
                for candidate_id, parent_ids, archive_class, decision in rows
            ],
        }

