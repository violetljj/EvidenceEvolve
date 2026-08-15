from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.hashing import sha256_bytes, sha256_file, sha256_object
from evidence_evolve.proposals.non_inferiority import (
    load_and_validate_p2_r1_protocol,
)
from evidence_evolve.proposals.p2_r1_execution import (
    P2R1Schedule,
    build_p2_r1_schedule,
)
from evidence_evolve.proposals.p2_r1_transport import (
    ExecutionProtocolViolation,
    TransportAttemptLimitReached,
    TransportLedger,
    TransportLedgerRecord,
)


REPO = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPO / "research/parity/shinka_native_p2_r1.protocol.json"
SYSTEM_SHA = sha256_bytes(b"system")
USER_SHA = sha256_bytes(b"request")


def _protocol():
    return load_and_validate_p2_r1_protocol(PROTOCOL_PATH, repo=REPO)


def _ledger(tmp_path: Path) -> TransportLedger:
    start = tmp_path / "start_manifest.json"
    if not start.exists():
        create_once_json(start, {"remote_calls_permitted": True})
    return TransportLedger(
        audit_dir=tmp_path / "audit",
        run_id="p2-r1-b01-official",
        block=1,
        arm="official",
        paired_local_seed=2026081501,
        state_namespace="p2-r1:block-01:arm-official",
        protocol_sha256="a" * 64,
        executor_commit="b" * 40,
        start_manifest_path=start,
        start_manifest_sha256=sha256_file(start),
        results_dir=tmp_path / "results",
    )


def _slot(ledger: TransportLedger, payload: str = "c" * 64) -> int:
    slot, action = ledger.begin_slot(
        system_prompt_sha256=SYSTEM_SHA,
        user_prompt_sha256=USER_SHA,
        payload_sha256=payload,
        model="headless/codex@gpt-5.6-terra?effort=high",
    )
    assert action == "REMOTE"
    return slot


def test_schedule_is_exactly_reconstructed_from_protocol(tmp_path: Path) -> None:
    protocol = _protocol()
    schedule = build_p2_r1_schedule(protocol, run_root=tmp_path)

    expected = []
    for block in protocol.design.schedule:
        for position, arm in enumerate(block.order, start=1):
            expected.append((block.block, arm, position, block.local_seed))

    assert len(schedule.runs) == 20
    assert [(run.block, run.arm, run.position_in_block, run.paired_local_seed) for run in schedule.runs] == expected
    assert [run.sequence for run in schedule.runs] == list(range(1, 21))
    assert sum(len(run.slots) for run in schedule.runs) == 100
    assert all(slot.model == "gpt-5.6-terra" for run in schedule.runs for slot in run.slots)


def test_schedule_rejects_injected_namespace_or_database_collision(tmp_path: Path) -> None:
    payload = build_p2_r1_schedule(_protocol(), run_root=tmp_path).model_dump()
    payload["runs"][1]["state_namespace"] = payload["runs"][0]["state_namespace"]
    with pytest.raises(ValidationError, match="namespace"):
        P2R1Schedule.model_validate(payload)

    payload = build_p2_r1_schedule(_protocol(), run_root=tmp_path).model_dump()
    payload["runs"][1]["database_path"] = payload["runs"][0]["database_path"]
    with pytest.raises(ValidationError, match="database_path collision"):
        P2R1Schedule.model_validate(payload)


def test_transport_retry_is_three_identical_attempts_and_one_slot(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    slot = _slot(ledger)

    async def fail() -> None:
        raise ConnectionError("transient")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            asyncio.run(
                ledger.call_transport(
                    slot_number=slot,
                    payload_sha256="c" * 64,
                    call=fail,
                )
            )

    record = TransportLedgerRecord.model_validate_json(ledger.path.read_text())
    assert len(record.slots) == 1
    assert len(record.slots[0].attempts) == 3
    assert record.slots[0].slot_id == "p2-r1-b01-official-slot-01"
    assert record.slots[0].block == 1
    assert record.slots[0].arm == "official"
    assert record.slots[0].paired_local_seed == 2026081501
    assert record.slots[0].state_namespace == "p2-r1:block-01:arm-official"
    assert record.slots[0].executor_commit == "b" * 40
    assert record.slots[0].protocol_sha256 == "a" * 64
    assert {attempt.payload_sha256 for attempt in record.slots[0].attempts} == {"c" * 64}
    assert record.slots[0].transport_state == "EXHAUSTED"

    with pytest.raises(TransportAttemptLimitReached, match="fourth"):
        ledger.begin_attempt(slot, "c" * 64)


def test_transport_payload_mutation_hard_fails_without_attempt(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    slot = _slot(ledger)

    with pytest.raises(ExecutionProtocolViolation, match="mutated"):
        ledger.begin_attempt(slot, "f" * 64)

    record = TransportLedgerRecord.model_validate_json(ledger.path.read_text())
    assert len(record.slots) == 1
    assert record.slots[0].attempts == []
    assert record.slots[0].transport_state == "PROTOCOL_VIOLATION"


def test_crash_resume_replays_durable_response_without_remote_resampling(
    tmp_path: Path,
) -> None:
    shinka = pytest.importorskip("shinka.llm.providers")
    ledger = _ledger(tmp_path)
    slot = _slot(ledger)
    response = shinka.QueryResult(
        content="proposal",
        msg="request",
        system_msg="system",
        new_msg_history=[],
        model_name="headless/codex@gpt-5.6-terra?effort=high",
        kwargs={},
        input_tokens=10,
        output_tokens=5,
    )
    remote_calls = 0

    async def succeed():
        nonlocal remote_calls
        remote_calls += 1
        return response

    returned = asyncio.run(
        ledger.call_transport(
            slot_number=slot,
            payload_sha256="c" * 64,
            call=succeed,
        )
    )
    assert returned.content == "proposal"
    assert remote_calls == 1

    resumed = _ledger(tmp_path)
    resumed_slot, action = resumed.begin_slot(
        system_prompt_sha256=SYSTEM_SHA,
        user_prompt_sha256=USER_SHA,
        payload_sha256="c" * 64,
        model="headless/codex@gpt-5.6-terra?effort=high",
    )
    assert resumed_slot == 1
    assert action == "REPLAY_SUCCESS"
    assert resumed.replay_response(resumed_slot).content == "proposal"
    assert remote_calls == 1


def test_unresolved_attempt_consumes_one_attempt_on_resume(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    slot = _slot(ledger)
    assert ledger.begin_attempt(slot, "c" * 64) == 1

    resumed = _ledger(tmp_path)
    record = TransportLedgerRecord.model_validate_json(resumed.path.read_text())
    assert record.slots[0].attempts[0].status == "UNRESOLVED"
    assert resumed.begin_attempt(slot, "c" * 64) == 2
    assert resumed.begin_attempt(slot, "c" * 64) == 3
    with pytest.raises(TransportAttemptLimitReached, match="fourth"):
        resumed.begin_attempt(slot, "c" * 64)


def test_crash_after_response_snapshot_recovers_without_another_call(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    slot = _slot(ledger)
    ledger.begin_attempt(slot, "c" * 64)
    create_once_json(
        ledger.responses_dir / "slot_01.json",
        {
            "content": "durable",
            "msg": "request",
            "system_msg": "system",
            "new_msg_history": [],
            "model_name": "headless/codex@gpt-5.6-terra?effort=high",
            "kwargs": {},
            "input_tokens": 1,
            "output_tokens": 1,
            "thinking_tokens": 0,
            "cost": 0.0,
            "input_cost": 0.0,
            "output_cost": 0.0,
            "thought": "",
            "model_posteriors": {},
            "num_tool_calls": 0,
            "num_total_queries": 1,
        },
    )

    resumed = _ledger(tmp_path)
    record = TransportLedgerRecord.model_validate_json(resumed.path.read_text())
    assert record.slots[0].attempts[0].status == "SUCCEEDED"
    assert record.slots[0].transport_state == "SUCCEEDED"


def test_database_receipt_completes_slot_and_resume_advances_deterministically(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _slot(ledger)
    results = tmp_path / "results"
    results.mkdir()
    connection = sqlite3.connect(results / "programs.sqlite")
    try:
        connection.executescript(
            "CREATE TABLE programs (generation INTEGER);"
            "CREATE TABLE attempt_log (generation INTEGER);"
            "INSERT INTO attempt_log VALUES (1);"
        )
        connection.commit()
    finally:
        connection.close()

    second, action = ledger.begin_slot(
        system_prompt_sha256=SYSTEM_SHA,
        user_prompt_sha256=USER_SHA,
        payload_sha256="c" * 64,
        model="headless/codex@gpt-5.6-terra?effort=high",
    )
    assert (second, action) == (2, "REMOTE")
    record = TransportLedgerRecord.model_validate_json(ledger.path.read_text())
    assert record.slots[0].scientific_state == "COMPLETE"
    assert len(record.slots) == 2


def test_start_manifest_must_exist_and_match_before_ledger_creation(tmp_path: Path) -> None:
    absent = tmp_path / "missing.json"
    with pytest.raises(ExecutionProtocolViolation, match="start manifest"):
        TransportLedger(
            audit_dir=tmp_path / "audit",
            run_id="p2-r1-b01-official",
            block=1,
            arm="official",
            paired_local_seed=2026081501,
            state_namespace="p2-r1:block-01:arm-official",
            protocol_sha256="a" * 64,
            executor_commit="b" * 40,
            start_manifest_path=absent,
            start_manifest_sha256="c" * 64,
            results_dir=tmp_path / "results",
        )
