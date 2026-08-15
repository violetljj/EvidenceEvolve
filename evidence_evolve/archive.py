from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evidence_evolve.meta_evolution.policy import AcquisitionDecision, CandidateAcquisition
from evidence_evolve.models import CandidateGenome, ReceiptEnvelope
from evidence_evolve.research_memory import (
    MemoryKind,
    MemoryRole,
    ResearchMemoryStore,
    RoleScopedMemoryPacket,
)
from evidence_evolve.understanding.signatures import MechanismAssessment


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
                CREATE TABLE IF NOT EXISTS acquisition_decisions (
                    generation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    acquisition_score REAL,
                    reasons_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    PRIMARY KEY(generation_id, candidate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_acquisition_generation
                    ON acquisition_decisions(generation_id, eligible, acquisition_score);
                CREATE TABLE IF NOT EXISTS mechanism_assessments (
                    receipt_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    support TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    assessment_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(receipt_id) REFERENCES evaluation_receipts(receipt_id)
                );
                CREATE INDEX IF NOT EXISTS idx_mechanism_candidate
                    ON mechanism_assessments(candidate_id, support);
                """
            )
        self.research_memory = ResearchMemoryStore(database)

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
            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, parent_ids_json, island, family, mutation_type,
                    archive_class, gate_decision, scientific_outcome, receipt_path,
                    receipt_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    parent_ids_json=excluded.parent_ids_json,
                    island=excluded.island,
                    family=excluded.family,
                    mutation_type=excluded.mutation_type,
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

    def record_acquisition(
        self,
        *,
        generation_id: str,
        policy_id: str,
        pool: list[CandidateAcquisition],
        decisions: list[AcquisitionDecision],
        created_at_utc: str,
    ) -> None:
        candidates = {item.candidate.candidate_id: item.candidate for item in pool}
        if set(candidates) != {decision.candidate_id for decision in decisions}:
            raise ValueError("acquisition decisions do not match candidate pool")
        with self._connect() as connection:
            for decision in decisions:
                values = (
                    generation_id,
                    decision.candidate_id,
                    policy_id,
                    json.dumps(
                        candidates[decision.candidate_id].model_dump(mode="json"),
                        sort_keys=True,
                    ),
                    int(decision.eligible),
                    decision.acquisition_score,
                    json.dumps(decision.reasons, sort_keys=True),
                    created_at_utc,
                )
                try:
                    connection.execute(
                        """
                        INSERT INTO acquisition_decisions(
                            generation_id, candidate_id, policy_id, candidate_json,
                            eligible, acquisition_score, reasons_json, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                except sqlite3.IntegrityError:
                    existing = connection.execute(
                        """
                        SELECT generation_id, candidate_id, policy_id, candidate_json,
                               eligible, acquisition_score, reasons_json
                        FROM acquisition_decisions
                        WHERE generation_id = ? AND candidate_id = ?
                        """,
                        (generation_id, decision.candidate_id),
                    ).fetchone()
                    if existing != values[:-1]:
                        raise

    def record_mechanism_assessment(
        self,
        *,
        receipt_id: str,
        assessment: MechanismAssessment,
        created_at_utc: str,
    ) -> None:
        values = (
            receipt_id,
            assessment.candidate_id,
            assessment.support.value,
            assessment.authority,
            json.dumps(assessment.model_dump(mode="json"), sort_keys=True),
            created_at_utc,
        )
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO mechanism_assessments(
                        receipt_id, candidate_id, support, authority,
                        assessment_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT receipt_id, candidate_id, support, authority,
                           assessment_json, created_at_utc
                    FROM mechanism_assessments WHERE receipt_id = ?
                    """,
                    (receipt_id,),
                ).fetchone()
                if existing != values:
                    raise
        self.research_memory.compile_receipt(receipt_id)

    def research_memory_packet(
        self,
        *,
        role: MemoryRole,
        query: str | None = None,
        campaign: str | None = None,
        family: str | None = None,
        kinds: set[MemoryKind] | None = None,
        limit: int = 8,
    ) -> RoleScopedMemoryPacket:
        """Compile and retrieve a role-scoped, scheduling-only memory packet."""
        self.research_memory.compile_history()
        return self.research_memory.retrieve_packet(
            role=role,
            query=query,
            campaign=campaign,
            family=family,
            kinds=kinds,
            limit=limit,
        )

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
            by_mechanism_support = dict(
                connection.execute(
                    "SELECT support, COUNT(*) FROM mechanism_assessments "
                    "GROUP BY support ORDER BY support"
                ).fetchall()
            )
            scheduled = connection.execute(
                "SELECT COUNT(*) FROM acquisition_decisions WHERE eligible = 1"
            ).fetchone()[0]
        return {
            "total": total,
            "unique_candidates": len({row[1] for row in rows}),
            "by_archive_class": by_class,
            "by_mechanism_support": by_mechanism_support,
            "scheduled_candidates": scheduled,
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

    def scientific_memory(self, limit: int = 20) -> list[dict[str, object]]:
        """Compile receipts into context that a later researcher can actually use."""
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT er.candidate_id, er.parent_ids_json, er.island, er.family,
                       er.mutation_type, er.archive_class, er.gate_decision,
                       er.scientific_outcome,
                       (
                           SELECT ad.candidate_json
                           FROM acquisition_decisions AS ad
                           WHERE ad.candidate_id = er.candidate_id
                           ORDER BY ad.created_at_utc DESC
                           LIMIT 1
                       ) AS candidate_json,
                       ma.assessment_json
                FROM evaluation_receipts AS er
                LEFT JOIN mechanism_assessments AS ma
                  ON ma.receipt_id = er.receipt_id
                ORDER BY er.created_at_utc DESC, er.receipt_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        memory: list[dict[str, object]] = []
        for (
            candidate_id,
            parent_ids_json,
            island,
            family,
            mutation_type,
            archive_class,
            gate_decision,
            scientific_outcome,
            candidate_json,
            assessment_json,
        ) in rows:
            acquisition = json.loads(candidate_json) if candidate_json else {}
            candidate = acquisition.get("candidate", acquisition)
            assessment = json.loads(assessment_json) if assessment_json else {}
            memory.append(
                {
                    "candidate_id": candidate_id,
                    "parent_ids": json.loads(parent_ids_json),
                    "genetic_parent_id": candidate.get("genetic_parent_id"),
                    "island": island,
                    "family": family,
                    "mutation_type": mutation_type,
                    "hypothesis": candidate.get("hypothesis"),
                    "intervention": candidate.get("intervention"),
                    "mechanism_claims": candidate.get("mechanism_claims", []),
                    "assumptions": candidate.get("assumptions", []),
                    "failure_risks": candidate.get("failure_risks", []),
                    "behavior_descriptor": candidate.get("behavior_descriptor", {}),
                    "scientific_outcome": scientific_outcome,
                    "archive_class": archive_class,
                    "gate_decision": gate_decision,
                    "mechanism_support": assessment.get("support"),
                    "mechanism_reasons": assessment.get("reasons", []),
                }
            )
        return memory

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
