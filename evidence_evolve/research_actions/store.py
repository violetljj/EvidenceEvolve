from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.budgets import BudgetLedger
from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.hashing import sha256_object
from evidence_evolve.research_actions.intelligence import ActionAuthorityRequired
from evidence_evolve.research_actions.models import (
    ActionExecutionResult,
    ActionOutcome,
    ActionReceiptEnvelope,
    ActionRunResult,
    ActionState,
    ResearchActionJob,
    ResearchActionReceipt,
)


class ResearchActionExecutor(Protocol):
    def execute(
        self, job: ResearchActionJob, action_dir: Path
    ) -> ActionExecutionResult: ...


class ResearchActionRunner:
    """Persist and execute non-code research actions with create-once receipts."""

    def __init__(
        self,
        *,
        database: Path,
        run_dir: Path,
        budgets: BudgetLedger,
    ) -> None:
        self.database = database
        self.run_dir = run_dir.resolve()
        self.budgets = budgets
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_action_jobs (
                    action_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    generation_id TEXT,
                    state TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    reason TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_action_jobs_campaign
                    ON research_action_jobs(campaign_id, state, action);
                CREATE TABLE IF NOT EXISTS research_action_receipts (
                    action_receipt_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL UNIQUE,
                    outcome TEXT NOT NULL,
                    receipt_path TEXT NOT NULL UNIQUE,
                    receipt_sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(action_id) REFERENCES research_action_jobs(action_id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def run(
        self,
        job: ResearchActionJob,
        executor: ResearchActionExecutor,
    ) -> ActionRunResult:
        self._record_job(job)
        existing = self._load_existing_receipt(job.action_id)
        if existing is not None:
            return ActionRunResult(
                state=self._terminal_state(existing.receipt.outcome),
                receipt=existing,
            )

        preflight = getattr(executor, "preflight", None)
        if callable(preflight):
            try:
                preflight(job)
            except ActionAuthorityRequired as exc:
                self._set_state(
                    job.action_id, ActionState.WAITING_FOR_AUTHORITY, str(exc)
                )
                return ActionRunResult(
                    state=ActionState.WAITING_FOR_AUTHORITY,
                    reason=str(exc),
                )

        self._reserve(job)
        started = datetime.now(timezone.utc).isoformat()
        self._set_state(job.action_id, ActionState.RUNNING)
        action_dir = self.run_dir / "actions" / job.action_id
        action_dir.mkdir(parents=True, exist_ok=True)
        try:
            execution = executor.execute(job, action_dir)
        except ActionAuthorityRequired as exc:
            self._set_state(
                job.action_id, ActionState.WAITING_FOR_AUTHORITY, str(exc)
            )
            return ActionRunResult(
                state=ActionState.WAITING_FOR_AUTHORITY,
                reason=str(exc),
            )
        except Exception as exc:
            execution = ActionExecutionResult(
                outcome=ActionOutcome.FAILED,
                issues=[f"ACTION_EXECUTION_FAILED:{type(exc).__name__}:{exc}"],
            )

        completed = datetime.now(timezone.utc).isoformat()
        receipt = ResearchActionReceipt(
            action_receipt_id=f"ACTION:{job.campaign_id}:{job.action_id}",
            job=job,
            started_at_utc=started,
            completed_at_utc=completed,
            outcome=execution.outcome,
            records=execution.records,
            artifacts=execution.artifacts,
            issues=execution.issues,
        )
        envelope = ActionReceiptEnvelope(
            receipt=receipt,
            receipt_sha256=sha256_object(receipt),
        )
        receipt_path = action_dir / "receipt.json"
        if receipt_path.exists():
            existing = ActionReceiptEnvelope.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
            if existing != envelope:
                raise RuntimeError(f"action receipt drift: {receipt_path}")
            envelope = existing
        else:
            create_once_json(receipt_path, envelope)
        relative = receipt_path.relative_to(self.run_dir).as_posix()
        receipt_json = json.dumps(envelope.model_dump(mode="json"), sort_keys=True)
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO research_action_receipts(
                        action_receipt_id, action_id, outcome, receipt_path,
                        receipt_sha256, receipt_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.action_receipt_id,
                        job.action_id,
                        receipt.outcome.value,
                        relative,
                        envelope.receipt_sha256,
                        receipt_json,
                        completed,
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT receipt_path, receipt_sha256, receipt_json
                    FROM research_action_receipts WHERE action_id = ?
                    """,
                    (job.action_id,),
                ).fetchone()
                if row != (relative, envelope.receipt_sha256, receipt_json):
                    raise
        state = self._terminal_state(receipt.outcome)
        self._set_state(job.action_id, state)
        from evidence_evolve.research_memory import ResearchMemoryStore

        ResearchMemoryStore(self.database).compile_action_receipt(
            receipt.action_receipt_id
        )
        return ActionRunResult(state=state, receipt=envelope)

    def _record_job(self, job: ResearchActionJob) -> None:
        now = datetime.now(timezone.utc).isoformat()
        job_json = json.dumps(job.model_dump(mode="json"), sort_keys=True)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_json FROM research_action_jobs WHERE action_id = ?",
                (job.action_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO research_action_jobs(
                        action_id, action, campaign_id, generation_id, state,
                        job_json, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.action_id,
                        job.action.value,
                        job.campaign_id,
                        job.generation_id,
                        ActionState.PLANNED.value,
                        job_json,
                        now,
                        now,
                    ),
                )
            elif row[0] != job_json:
                raise ValueError(f"research action job drift: {job.action_id}")

    def _set_state(
        self, action_id: str, state: ActionState, reason: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE research_action_jobs
                SET state = ?, reason = ?, updated_at_utc = ?
                WHERE action_id = ?
                """,
                (
                    state.value,
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                    action_id,
                ),
            )

    def _reserve(self, job: ResearchActionJob) -> None:
        if job.action is ResearchAction.SEARCH_LITERATURE:
            if job.max_papers:
                self.budgets.reserve(
                    "literature_searches",
                    1,
                    f"literature_searches:{job.action_id}",
                )
            if job.max_repositories:
                self.budgets.reserve(
                    "repository_inspections",
                    job.max_repositories,
                    f"repository_inspections:{job.action_id}",
                )
        elif job.action is ResearchAction.REPLICATE:
            self.budgets.reserve(
                "replications", 1, f"replications:{job.action_id}"
            )
        elif job.action is ResearchAction.ACQUIRE_EVIDENCE:
            self.budgets.reserve(
                "evidence_acquisitions",
                1,
                f"evidence_acquisitions:{job.action_id}",
            )
        else:
            raise ValueError(f"research action has no independent executor: {job.action}")

    def _load_existing_receipt(
        self, action_id: str
    ) -> ActionReceiptEnvelope | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT receipt_path, receipt_sha256
                FROM research_action_receipts WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        path = self.run_dir / row[0]
        envelope = ActionReceiptEnvelope.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        actual = sha256_object(envelope.receipt)
        if actual != envelope.receipt_sha256 or actual != row[1]:
            raise RuntimeError(f"action receipt hash mismatch: {action_id}")
        return envelope

    @staticmethod
    def _terminal_state(outcome: ActionOutcome) -> ActionState:
        if outcome in {ActionOutcome.SUCCEEDED, ActionOutcome.SUCCEEDED_WITH_GAPS}:
            return ActionState.SUCCEEDED
        if outcome is ActionOutcome.NOT_EVALUABLE:
            return ActionState.NOT_EVALUABLE
        return ActionState.FAILED
