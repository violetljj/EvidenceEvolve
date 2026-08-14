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
                CREATE TABLE IF NOT EXISTS evaluation_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    parent_ids_json TEXT NOT NULL,
                    island TEXT NOT NULL,
                    family TEXT NOT NULL,
                    mutation_type TEXT NOT NULL,
                    archive_class TEXT NOT NULL,
                    gate_decision TEXT NOT NULL,
                    scientific_outcome TEXT NOT NULL,
                    receipt_path TEXT NOT NULL UNIQUE,
                    receipt_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evaluation_receipts_archive
                    ON evaluation_receipts(archive_class, island);
                CREATE INDEX IF NOT EXISTS idx_evaluation_receipts_candidate
                    ON evaluation_receipts(candidate_id, created_at_utc);
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
            values = (
                envelope.receipt.receipt_id,
                candidate.candidate_id,
                envelope.receipt.evaluation_input.stage.value,
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
            )
            try:
                connection.execute(
                    """
                    INSERT INTO evaluation_receipts(
                        receipt_id, candidate_id, stage, parent_ids_json, island,
                        family, mutation_type, archive_class, gate_decision,
                        scientific_outcome, receipt_path, receipt_sha256,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT receipt_path, receipt_sha256 FROM evaluation_receipts "
                    "WHERE receipt_id = ?",
                    (envelope.receipt.receipt_id,),
                ).fetchone()
                if existing != (receipt_path.as_posix(), envelope.receipt_sha256):
                    raise

    def summary(self) -> dict[str, object]:
        with self._connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM evaluation_receipts"
            ).fetchone()[0]
            if total == 0:
                return self._legacy_summary(connection)
            by_class = dict(
                connection.execute(
                    "SELECT archive_class, COUNT(*) FROM evaluation_receipts "
                    "GROUP BY archive_class ORDER BY archive_class"
                ).fetchall()
            )
            rows = connection.execute(
                "SELECT receipt_id, candidate_id, stage, parent_ids_json, "
                "archive_class, gate_decision FROM evaluation_receipts "
                "ORDER BY created_at_utc, receipt_id"
            ).fetchall()
        return {
            "total": total,
            "unique_candidates": len({row[1] for row in rows}),
            "by_archive_class": by_class,
            "candidates": [
                {
                    "receipt_id": receipt_id,
                    "candidate_id": candidate_id,
                    "stage": stage,
                    "parent_ids": json.loads(parent_ids),
                    "archive_class": archive_class,
                    "gate_decision": decision,
                }
                for receipt_id, candidate_id, stage, parent_ids, archive_class, decision in rows
            ],
        }

    @staticmethod
    def _legacy_summary(connection: sqlite3.Connection) -> dict[str, object]:
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
            "unique_candidates": total,
            "legacy_schema": True,
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
