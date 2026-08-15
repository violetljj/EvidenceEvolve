from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.hashing import sha256_bytes, sha256_file, sha256_object
from evidence_evolve.models import StrictModel


MAX_TRANSPORT_ATTEMPTS = 3
MAX_SCIENTIFIC_SLOTS = 5
AUDIT_DIR_ENV = "EVIDENCE_EVOLVE_P2_R1_AUDIT_DIR"
RUN_ID_ENV = "EVIDENCE_EVOLVE_P2_R1_RUN_ID"
START_MANIFEST_ENV = "EVIDENCE_EVOLVE_P2_R1_START_MANIFEST"
START_MANIFEST_SHA_ENV = "EVIDENCE_EVOLVE_P2_R1_START_MANIFEST_SHA256"
PROTOCOL_SHA_ENV = "EVIDENCE_EVOLVE_P2_R1_PROTOCOL_SHA256"
EXECUTOR_COMMIT_ENV = "EVIDENCE_EVOLVE_P2_R1_EXECUTOR_COMMIT"
RESULTS_DIR_ENV = "EVIDENCE_EVOLVE_P2_R1_RESULTS_DIR"
MODEL_ENV = "EVIDENCE_EVOLVE_P2_R1_MODEL"
BLOCK_ENV = "EVIDENCE_EVOLVE_P2_R1_BLOCK"
ARM_ENV = "EVIDENCE_EVOLVE_P2_R1_ARM"
STATE_NAMESPACE_ENV = "EVIDENCE_EVOLVE_P2_R1_STATE_NAMESPACE"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionProtocolViolation(BaseException):
    """A non-retryable execution violation that upstream retry loops cannot catch."""


class TransportAttemptLimitReached(RuntimeError):
    """A retryable local sentinel that never reaches the remote transport."""


class TransportAttemptRecord(StrictModel):
    attempt: int = Field(ge=1, le=MAX_TRANSPORT_ATTEMPTS)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["STARTED", "FAILED", "SUCCEEDED", "UNRESOLVED"]
    started_at: str
    finished_at: str | None = None
    error_type: str | None = None
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response_path: str | None = None


class ScientificSlotRecord(StrictModel):
    slot_id: str
    slot: int = Field(ge=1, le=MAX_SCIENTIFIC_SLOTS)
    block: int = Field(ge=1, le=10)
    arm: Literal["official", "native"]
    paired_local_seed: int
    state_namespace: str
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendered_system_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendered_user_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str
    transport_state: Literal[
        "PENDING", "SUCCEEDED", "EXHAUSTED", "PROTOCOL_VIOLATION"
    ] = "PENDING"
    scientific_state: Literal["PENDING", "COMPLETE"] = "PENDING"
    terminal_funnel_state: Literal[
        "MODEL_INVOCATION_NOT_STARTED",
        "MODEL_RESPONSE_MISSING",
        "PROPOSAL_EXTRACTION_FAILED",
        "MATERIALIZATION_FAILED",
        "COMPILE_FAILED",
        "EVALUATOR_NOT_REACHED",
        "EVALUATOR_INVALID",
        "EVALUATOR_VALID_NOT_USEFUL",
        "USEFUL",
    ] | None = None
    attempts: list[TransportAttemptRecord] = Field(
        default_factory=list, max_length=MAX_TRANSPORT_ATTEMPTS
    )

    @model_validator(mode="after")
    def retries_are_identical(self) -> "ScientificSlotRecord":
        if any(
            attempt.payload_sha256 != self.request_payload_sha256
            for attempt in self.attempts
        ):
            raise ValueError("transport retry changed request payload")
        if [attempt.attempt for attempt in self.attempts] != list(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("transport attempts must be consecutive")
        return self


class TransportLedgerRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    block: int = Field(ge=1, le=10)
    arm: Literal["official", "native"]
    paired_local_seed: int
    state_namespace: str
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    start_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    slots: list[ScientificSlotRecord] = Field(
        default_factory=list, max_length=MAX_SCIENTIFIC_SLOTS
    )


def request_payload(
    *,
    msg: str,
    system_msg: str,
    msg_history: list[dict[str, Any]],
    llm_kwargs: dict[str, Any],
    output_model: Any,
    model_posterior: list[float] | None,
) -> dict[str, Any]:
    output_schema = None
    if output_model is not None:
        schema_method = getattr(output_model, "model_json_schema", None)
        output_schema = schema_method() if callable(schema_method) else str(output_model)
    return {
        "msg": msg,
        "system_msg": system_msg,
        "msg_history": msg_history,
        "llm_kwargs": llm_kwargs,
        "output_schema": output_schema,
        "model_posterior": model_posterior,
    }


class TransportLedger:
    def __init__(
        self,
        *,
        audit_dir: Path,
        run_id: str,
        block: int,
        arm: Literal["official", "native"],
        paired_local_seed: int,
        state_namespace: str,
        protocol_sha256: str,
        executor_commit: str,
        start_manifest_path: Path,
        start_manifest_sha256: str,
        results_dir: Path,
    ) -> None:
        self.audit_dir = audit_dir.resolve()
        self.path = self.audit_dir / "transport_ledger.json"
        self.responses_dir = self.audit_dir / "responses"
        self.results_dir = results_dir.resolve()
        self._lock = threading.RLock()
        if not start_manifest_path.is_file():
            raise ExecutionProtocolViolation("start manifest is absent before remote call")
        if sha256_file(start_manifest_path) != start_manifest_sha256:
            raise ExecutionProtocolViolation("start manifest hash changed before remote call")
        expected = TransportLedgerRecord(
            run_id=run_id,
            block=block,
            arm=arm,
            paired_local_seed=paired_local_seed,
            state_namespace=state_namespace,
            protocol_sha256=protocol_sha256,
            executor_commit=executor_commit,
            start_manifest_sha256=start_manifest_sha256,
        )
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            actual = self._read()
            if actual.model_copy(update={"slots": []}) != expected:
                raise ExecutionProtocolViolation("transport ledger identity mismatch")
            self._normalize_unresolved(actual)
        else:
            create_once_json(self.path, expected)

    def _read(self) -> TransportLedgerRecord:
        return TransportLedgerRecord.model_validate_json(
            self.path.read_text(encoding="utf-8")
        )

    def _write(self, record: TransportLedgerRecord) -> None:
        atomic_write_json(self.path, record)

    def _normalize_unresolved(self, record: TransportLedgerRecord) -> None:
        changed = False
        for slot in record.slots:
            for attempt in slot.attempts:
                if attempt.status == "STARTED":
                    response_path = self.responses_dir / f"slot_{slot.slot:02d}.json"
                    if response_path.is_file():
                        response_payload = json.loads(
                            response_path.read_text(encoding="utf-8")
                        )
                        self._validate_response_identity(slot, response_payload)
                        attempt.status = "SUCCEEDED"
                        attempt.response_sha256 = sha256_object(response_payload)
                        attempt.response_path = str(response_path)
                        slot.transport_state = "SUCCEEDED"
                    else:
                        attempt.status = "UNRESOLVED"
                        attempt.error_type = "PROCESS_INTERRUPTED"
                    attempt.finished_at = _utc_now()
                    changed = True
        if changed:
            self._write(record)

    def _database_terminal_generations(self) -> set[int]:
        database = self.results_dir / "programs.sqlite"
        if not database.is_file():
            return set()
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            program_generations = {
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT generation FROM programs WHERE generation > 0"
                )
            }
            failed_generations = {
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT generation FROM attempt_log WHERE generation > 0"
                )
            }
            return program_generations | failed_generations
        except sqlite3.OperationalError:
            return set()
        finally:
            connection.close()

    def reconcile_scientific_state(self) -> None:
        with self._lock:
            record = self._read()
            terminal = self._database_terminal_generations()
            changed = False
            for slot in record.slots:
                if slot.slot in terminal and slot.scientific_state != "COMPLETE":
                    slot.scientific_state = "COMPLETE"
                    changed = True
            if changed:
                self._write(record)

    def validate_baseline(
        self, expected_initial_sha256: str, expected_score: float
    ) -> float:
        database = self.results_dir / "programs.sqlite"
        if not database.is_file():
            raise ExecutionProtocolViolation("baseline database is absent before first call")
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT code, combined_score FROM programs WHERE generation = 0"
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            raise ExecutionProtocolViolation("baseline row is absent before first call")
        hashes = {sha256_bytes(str(row[0]).encode("utf-8")) for row in rows}
        scores = {float(row[1]) for row in rows}
        if hashes != {expected_initial_sha256}:
            raise ExecutionProtocolViolation("baseline code does not match frozen incumbent")
        if len(scores) != 1:
            raise ExecutionProtocolViolation("resume produced inconsistent baseline scores")
        actual_score = next(iter(scores))
        if actual_score != expected_score:
            raise ExecutionProtocolViolation(
                "baseline score differs from frozen admission baseline"
            )
        return actual_score

    def begin_slot(
        self,
        *,
        system_prompt_sha256: str,
        user_prompt_sha256: str,
        payload_sha256: str,
        model: str,
    ) -> tuple[int, Literal["REMOTE", "REPLAY_SUCCESS", "REPLAY_EXHAUSTED"]]:
        with self._lock:
            self.reconcile_scientific_state()
            record = self._read()
            pending = next(
                (slot for slot in record.slots if slot.scientific_state == "PENDING"),
                None,
            )
            if pending is not None:
                expected = (
                    pending.rendered_system_prompt_sha256,
                    pending.rendered_user_prompt_sha256,
                    pending.request_payload_sha256,
                    pending.model,
                )
                actual = (
                    system_prompt_sha256,
                    user_prompt_sha256,
                    payload_sha256,
                    model,
                )
                if expected != actual:
                    pending.transport_state = "PROTOCOL_VIOLATION"
                    self._write(record)
                    raise ExecutionProtocolViolation(
                        "resume or retry mutated the pending scientific-slot payload"
                    )
                if pending.transport_state == "SUCCEEDED":
                    return pending.slot, "REPLAY_SUCCESS"
                if pending.transport_state == "EXHAUSTED":
                    return pending.slot, "REPLAY_EXHAUSTED"
                if pending.transport_state == "PROTOCOL_VIOLATION":
                    raise ExecutionProtocolViolation(
                        "scientific slot is closed by a protocol violation"
                    )
                return pending.slot, "REMOTE"
            if len(record.slots) >= MAX_SCIENTIFIC_SLOTS:
                raise ExecutionProtocolViolation(
                    "a sixth scientific proposal slot is forbidden"
                )
            slot = ScientificSlotRecord(
                slot_id=f"{record.run_id}-slot-{len(record.slots) + 1:02d}",
                slot=len(record.slots) + 1,
                block=record.block,
                arm=record.arm,
                paired_local_seed=record.paired_local_seed,
                state_namespace=record.state_namespace,
                executor_commit=record.executor_commit,
                protocol_sha256=record.protocol_sha256,
                rendered_system_prompt_sha256=system_prompt_sha256,
                rendered_user_prompt_sha256=user_prompt_sha256,
                request_payload_sha256=payload_sha256,
                model=model,
            )
            record.slots.append(slot)
            self._write(record)
            return slot.slot, "REMOTE"

    def begin_attempt(self, slot_number: int, payload_sha256: str) -> int:
        with self._lock:
            record = self._read()
            slot = record.slots[slot_number - 1]
            if payload_sha256 != slot.request_payload_sha256:
                slot.transport_state = "PROTOCOL_VIOLATION"
                self._write(record)
                raise ExecutionProtocolViolation("transport retry payload mutated")
            if len(slot.attempts) >= MAX_TRANSPORT_ATTEMPTS:
                raise TransportAttemptLimitReached(
                    "fourth transport attempt is forbidden"
                )
            attempt = TransportAttemptRecord(
                attempt=len(slot.attempts) + 1,
                payload_sha256=payload_sha256,
                status="STARTED",
                started_at=_utc_now(),
            )
            slot.attempts.append(attempt)
            self._write(record)
            return attempt.attempt

    def fail_attempt(self, slot_number: int, attempt_number: int, error: BaseException) -> None:
        with self._lock:
            record = self._read()
            slot = record.slots[slot_number - 1]
            attempt = slot.attempts[attempt_number - 1]
            attempt.status = "FAILED"
            attempt.finished_at = _utc_now()
            attempt.error_type = type(error).__name__
            if len(slot.attempts) == MAX_TRANSPORT_ATTEMPTS:
                slot.transport_state = "EXHAUSTED"
            self._write(record)

    def succeed_attempt(
        self, slot_number: int, attempt_number: int, response: Any
    ) -> None:
        response_payload = response.to_dict()
        record = self._read()
        slot = record.slots[slot_number - 1]
        self._validate_response_identity(slot, response_payload)
        response_path = self.responses_dir / f"slot_{slot_number:02d}.json"
        response_sha256 = sha256_object(response_payload)
        if response_path.exists():
            if sha256_file(response_path) != sha256_bytes(
                json.dumps(
                    response_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            ):
                raise ExecutionProtocolViolation("durable response snapshot changed")
        else:
            create_once_json(response_path, response_payload)
        with self._lock:
            record = self._read()
            slot = record.slots[slot_number - 1]
            attempt = slot.attempts[attempt_number - 1]
            attempt.status = "SUCCEEDED"
            attempt.finished_at = _utc_now()
            attempt.response_sha256 = response_sha256
            attempt.response_path = str(response_path)
            slot.transport_state = "SUCCEEDED"
            self._write(record)

    @staticmethod
    def _validate_response_identity(
        slot: ScientificSlotRecord, response_payload: dict[str, Any]
    ) -> None:
        if sha256_bytes(str(response_payload.get("msg", "")).encode("utf-8")) != (
            slot.rendered_user_prompt_sha256
        ):
            raise ExecutionProtocolViolation("response user prompt identity changed")
        if sha256_bytes(
            str(response_payload.get("system_msg", "")).encode("utf-8")
        ) != slot.rendered_system_prompt_sha256:
            raise ExecutionProtocolViolation("response system prompt identity changed")
        if response_payload.get("model_name") != slot.model:
            raise ExecutionProtocolViolation("response model identity changed")

    def replay_response(self, slot_number: int) -> Any:
        from shinka.llm.providers import QueryResult

        record = self._read()
        slot = record.slots[slot_number - 1]
        successful = next(
            (attempt for attempt in slot.attempts if attempt.status == "SUCCEEDED"),
            None,
        )
        if successful is None or successful.response_path is None:
            raise ExecutionProtocolViolation("successful slot lacks durable response")
        path = Path(successful.response_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if sha256_object(payload) != successful.response_sha256:
            raise ExecutionProtocolViolation("durable response hash mismatch")
        return QueryResult(**payload)

    async def call_transport(
        self,
        *,
        slot_number: int,
        payload_sha256: str,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        attempt = self.begin_attempt(slot_number, payload_sha256)
        try:
            response = await call()
        except BaseException as error:
            self.fail_attempt(slot_number, attempt, error)
            raise
        self.succeed_attempt(slot_number, attempt, response)
        return response


_CURRENT_SLOT: ContextVar[int | None] = ContextVar("p2_r1_slot", default=None)
_CURRENT_PAYLOAD: ContextVar[str | None] = ContextVar("p2_r1_payload", default=None)
_INSTALLED = False


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ExecutionProtocolViolation(f"required execution environment is absent: {name}")
    return value


def install_transport_audit_from_environment() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    audit_dir = os.environ.get(AUDIT_DIR_ENV)
    if not audit_dir:
        return
    ledger = TransportLedger(
        audit_dir=Path(audit_dir),
        run_id=_required_env(RUN_ID_ENV),
        block=int(_required_env(BLOCK_ENV)),
        arm=_required_env(ARM_ENV),
        paired_local_seed=int(_required_env("EVIDENCE_EVOLVE_P2_R1_SEED")),
        state_namespace=_required_env(STATE_NAMESPACE_ENV),
        protocol_sha256=_required_env(PROTOCOL_SHA_ENV),
        executor_commit=_required_env(EXECUTOR_COMMIT_ENV),
        start_manifest_path=Path(_required_env(START_MANIFEST_ENV)),
        start_manifest_sha256=_required_env(START_MANIFEST_SHA_ENV),
        results_dir=Path(_required_env(RESULTS_DIR_ENV)),
    )

    import shinka.llm.llm as llm_module
    from shinka.llm.kwargs import sample_model_kwargs

    original_high_level = llm_module.AsyncLLMClient.query
    original_transport = llm_module.query_async

    async def audited_high_level(
        client: Any,
        msg: str,
        system_msg: str,
        msg_history: list[dict[str, Any]] = [],
        llm_kwargs: dict[str, Any] | None = None,
        model_sample_probs: list[float] | None = None,
        model_posterior: list[float] | None = None,
    ) -> Any:
        posterior = (
            model_sample_probs
            if model_sample_probs is not None
            else client.model_sample_probs
        )
        if llm_kwargs is None:
            resolved_kwargs = sample_model_kwargs(
                model_names=client.model_names,
                temperatures=client.temperatures,
                max_tokens=client.max_tokens,
                reasoning_efforts=client.reasoning_efforts,
                model_sample_probs=posterior,
            )
        elif "model_name" not in llm_kwargs:
            sampled = sample_model_kwargs(
                model_names=client.model_names,
                temperatures=client.temperatures,
                max_tokens=client.max_tokens,
                reasoning_efforts=client.reasoning_efforts,
                model_sample_probs=posterior,
            )
            resolved_kwargs = {**sampled, **llm_kwargs}
        else:
            resolved_kwargs = dict(llm_kwargs)
        resolved_kwargs = client._attach_headless_work_dir(resolved_kwargs)
        payload = request_payload(
            msg=msg,
            system_msg=system_msg,
            msg_history=msg_history,
            llm_kwargs=resolved_kwargs,
            output_model=client.output_model,
            model_posterior=model_posterior,
        )
        payload_sha256 = sha256_object(payload)
        model = str(resolved_kwargs.get("model_name", ""))
        expected_model = _required_env(MODEL_ENV)
        if model != expected_model:
            raise ExecutionProtocolViolation(
                f"execution model changed: expected={expected_model} actual={model}"
            )
        expected_initial = _required_env("EVIDENCE_EVOLVE_P2_R1_INITIAL_SHA256")
        expected_score = float(_required_env("EVIDENCE_EVOLVE_P2_R1_BASELINE_SCORE"))
        ledger.validate_baseline(expected_initial, expected_score)
        slot, action = ledger.begin_slot(
            system_prompt_sha256=sha256_bytes(system_msg.encode("utf-8")),
            user_prompt_sha256=sha256_bytes(msg.encode("utf-8")),
            payload_sha256=payload_sha256,
            model=model,
        )
        if action == "REPLAY_SUCCESS":
            return ledger.replay_response(slot)
        if action == "REPLAY_EXHAUSTED":
            return None
        slot_token = _CURRENT_SLOT.set(slot)
        payload_token = _CURRENT_PAYLOAD.set(payload_sha256)
        try:
            return await original_high_level(
                client,
                msg,
                system_msg,
                msg_history,
                resolved_kwargs,
                model_sample_probs,
                model_posterior,
            )
        finally:
            _CURRENT_PAYLOAD.reset(payload_token)
            _CURRENT_SLOT.reset(slot_token)

    async def audited_transport(*args: Any, **kwargs: Any) -> Any:
        slot = _CURRENT_SLOT.get()
        expected_payload = _CURRENT_PAYLOAD.get()
        if slot is None or expected_payload is None:
            raise ExecutionProtocolViolation("transport call escaped scientific-slot audit")
        actual_payload = request_payload(
            msg=kwargs["msg"],
            system_msg=kwargs["system_msg"],
            msg_history=kwargs.get("msg_history", []),
            llm_kwargs={
                key: value
                for key, value in kwargs.items()
                if key
                not in {
                    "msg",
                    "system_msg",
                    "msg_history",
                    "output_model",
                    "model_posteriors",
                }
            },
            output_model=kwargs.get("output_model"),
            model_posterior=(
                list(kwargs["model_posteriors"].values())
                if kwargs.get("model_posteriors")
                else None
            ),
        )
        actual_sha256 = sha256_object(actual_payload)
        if actual_sha256 != expected_payload:
            raise ExecutionProtocolViolation("transport payload differs from slot payload")
        return await ledger.call_transport(
            slot_number=slot,
            payload_sha256=actual_sha256,
            call=lambda: original_transport(*args, **kwargs),
        )

    llm_module.AsyncLLMClient.query = audited_high_level
    llm_module.query_async = audited_transport
    _INSTALLED = True


__all__ = [
    "ExecutionProtocolViolation",
    "ScientificSlotRecord",
    "TransportLedger",
    "TransportLedgerRecord",
    "TransportAttemptLimitReached",
    "install_transport_audit_from_environment",
    "request_payload",
]
