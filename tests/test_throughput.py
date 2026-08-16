from __future__ import annotations

import threading
import time

from evidence_evolve.discovery.throughput import (
    AsyncFunnelEngine,
    CandidateTicket,
    FunnelCallbacks,
    FunnelDecision,
    FunnelStage,
    StageStatus,
    ThroughputPolicy,
)
from evidence_evolve.models import MechanicsStatus, ScientificOutcome


def _ticket(index: int, *, lineage: str | None = None) -> CandidateTicket:
    return CandidateTicket(
        candidate_id=f"CAND-{index:03d}",
        dispatch_index=index,
        lineage_id=lineage or f"LINEAGE-{index % 2}",
        operator_class="structural" if index % 2 == 0 else "local",
        genetic_parent_id="INCUMBENT",
        requires_structural_transition=index % 2 == 0,
    )


def _callbacks(
    *,
    active: dict[str, int] | None = None,
    lock: threading.Lock | None = None,
    admitted: list[str] | None = None,
    collapse_structural_roots: bool = False,
) -> FunnelCallbacks:
    def propose(ticket: CandidateTicket) -> dict[str, str]:
        if active is not None and lock is not None:
            with lock:
                active["current"] += 1
                active["maximum"] = max(active["maximum"], active["current"])
            time.sleep(0.01)
            with lock:
                active["current"] -= 1
        return {"candidate_id": ticket.candidate_id}

    def implement(ticket: CandidateTicket, proposal: object) -> dict[str, object]:
        return {"ticket": ticket, "proposal": proposal}

    def l0(ticket: CandidateTicket, artifact: object) -> FunnelDecision:
        return FunnelDecision(
            stage=FunnelStage.L0,
            status=StageStatus.PASS,
            continue_pipeline=True,
            mechanics_status=MechanicsStatus.PASS,
            data_eligible=False,
            scientific_outcome=ScientificOutcome.NOT_EVALUABLE_DATA,
            reason_codes=["MECHANICS_ONLY"],
        )

    def l1(
        ticket: CandidateTicket,
        artifact: object,
        prior: FunnelDecision,
    ) -> FunnelDecision:
        blocked = ticket.dispatch_index in {2, 3}
        return FunnelDecision(
            stage=FunnelStage.L1,
            status=StageStatus.BLOCK if blocked else StageStatus.PASS,
            continue_pipeline=not blocked,
            mechanics_status=MechanicsStatus.PASS,
            data_eligible=True,
            controls={"candidate_valid": True, "development_only": True},
            metrics={"probe_speedup": 0.9 if blocked else 1.1},
            scientific_outcome=(
                ScientificOutcome.VALID_NEGATIVE
                if blocked
                else ScientificOutcome.POSITIVE_HEADROOM
            ),
            reason_codes=["FROZEN_L1_RULE"],
        )

    def l2(
        ticket: CandidateTicket,
        artifact: object,
        prior: FunnelDecision,
    ) -> FunnelDecision:
        promotion_worthy = ticket.dispatch_index != 1
        return FunnelDecision(
            stage=FunnelStage.L2,
            status=StageStatus.PASS,
            mechanics_status=MechanicsStatus.PASS,
            data_eligible=True,
            controls={"candidate_valid": True, "development_only": True},
            metrics={"raw_speedup": 1.2 if promotion_worthy else 0.8},
            scientific_outcome=(
                ScientificOutcome.POSITIVE_HEADROOM
                if promotion_worthy
                else ScientificOutcome.VALID_NEGATIVE
            ),
            admission_eligible=True,
            promotion_worthy=promotion_worthy,
            structural_transition_pass=ticket.requires_structural_transition,
            structural_root_key=(
                (
                    "shared-mechanism-family"
                    if collapse_structural_roots
                    else ticket.candidate_id
                )
                if ticket.requires_structural_transition
                else None
            ),
            incumbent_improved=ticket.dispatch_index == 4,
            reason_codes=["FULL_DEVELOPMENT"],
        )

    def admit(record) -> None:
        if admitted is not None:
            admitted.append(record.ticket.candidate_id)

    return FunnelCallbacks(
        propose=propose,
        implement=implement,
        l0=l0,
        l1=l1,
        l2=l2,
        admit=admit,
    )


def test_async_funnel_is_work_conserving_and_audits_false_blocks() -> None:
    active = {"current": 0, "maximum": 0}
    lock = threading.Lock()
    admitted: list[str] = []
    result = AsyncFunnelEngine(
        ThroughputPolicy(
            policy_id="THROUGHPUT-TEST",
            total_candidate_budget=4,
            propose_workers=4,
            implement_workers=2,
            l0_workers=2,
            l1_workers=2,
            l2_workers=2,
            max_inflight_per_lineage=2,
            operator_quotas={"local": 2, "structural": 2},
            shadow_audit_stride=2,
        ),
        _callbacks(active=active, lock=lock, admitted=admitted),
    ).run([_ticket(index) for index in range(1, 5)])

    assert active["maximum"] >= 2
    assert result.blind_artifacts_read is False
    assert result.confirmation_runs == 0
    assert result.metrics.proposed == 4
    assert result.metrics.implemented == 4
    assert result.metrics.l0_completed == 4
    assert result.metrics.l1_completed == 4
    assert result.metrics.l2_completed == 3
    assert result.metrics.l2_dev_valid == 3
    assert result.metrics.shadow_audits == 1
    assert result.metrics.false_blocks == 1
    assert result.metrics.false_clears == 1
    assert result.metrics.structural_roots == 2
    assert result.metrics.useful_candidates == 2
    assert result.metrics.incumbent_improvements == 1
    assert result.resources["process_tree_cpu_seconds"] >= 0.0
    assert result.resources["allocated_cpu_utilization_percent"] >= 0.0
    assert admitted == ["CAND-001", "CAND-004"]
    third = result.records[2]
    assert third.terminal_stage is FunnelStage.L1
    assert third.terminal_status == "BLOCKED"
    assert third.decision(FunnelStage.L2) is None


def test_structural_root_kpi_deduplicates_mechanism_family() -> None:
    result = AsyncFunnelEngine(
        ThroughputPolicy(
            policy_id="STRUCTURAL-DEDUP",
            total_candidate_budget=4,
            operator_quotas={"local": 2, "structural": 2},
            shadow_audit_stride=2,
        ),
        _callbacks(collapse_structural_roots=True),
    ).run([_ticket(index) for index in range(1, 5)])

    assert result.metrics.l2_dev_valid == 3
    assert result.metrics.structural_roots == 1
    assert result.metrics.useful_candidates == 1


def test_shadow_audit_never_promotes_invalid_mechanics() -> None:
    base = _callbacks()

    def invalid_l1(ticket, artifact, prior) -> FunnelDecision:
        del ticket, artifact, prior
        return FunnelDecision(
            stage=FunnelStage.L1,
            status=StageStatus.BLOCK,
            mechanics_status=MechanicsStatus.FAIL,
            data_eligible=False,
            controls={"candidate_valid": False, "development_only": True},
            scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
            reason_codes=["INVALID_SOLUTION"],
        )

    callbacks = FunnelCallbacks(
        propose=base.propose,
        implement=base.implement,
        l0=base.l0,
        l1=invalid_l1,
        l2=base.l2,
        admit=base.admit,
    )
    result = AsyncFunnelEngine(
        ThroughputPolicy(
            policy_id="NO-INVALID-SHADOW",
            total_candidate_budget=1,
            operator_quotas={"structural": 1},
            shadow_audit_stride=2,
        ),
        callbacks,
    ).run([_ticket(4)])

    assert result.metrics.shadow_audits == 0
    assert result.metrics.l2_completed == 0
    assert result.records[0].terminal_stage is FunnelStage.L1


def test_async_and_serial_funnels_have_decision_parity() -> None:
    tickets = [_ticket(index, lineage=f"L-{index}") for index in range(1, 7)]
    common = {
        "policy_id": "PARITY",
        "total_candidate_budget": 6,
        "max_inflight_per_lineage": 1,
        "shadow_audit_stride": 2,
    }
    serial = AsyncFunnelEngine(
        ThroughputPolicy(
            **common,
            propose_workers=1,
            implement_workers=1,
            l0_workers=1,
            l1_workers=1,
            l2_workers=1,
        ),
        _callbacks(),
    ).run(tickets)
    parallel = AsyncFunnelEngine(
        ThroughputPolicy(
            **common,
            propose_workers=4,
            implement_workers=3,
            l0_workers=3,
            l1_workers=3,
            l2_workers=2,
        ),
        _callbacks(),
    ).run(tickets)

    def normalized(result) -> list[dict[str, object]]:
        return [
            {
                "candidate_id": record.ticket.candidate_id,
                "decisions": [item.model_dump(mode="json") for item in record.decisions],
                "terminal_stage": record.terminal_stage.value,
                "terminal_status": record.terminal_status,
                "shadow_audit": record.shadow_audit,
                "admitted": record.admitted,
            }
            for record in result.records
        ]

    assert normalized(serial) == normalized(parallel)
    for field in (
        "proposed",
        "implemented",
        "l0_completed",
        "l1_completed",
        "l2_completed",
        "l2_dev_valid",
        "structural_roots",
        "useful_candidates",
        "incumbent_improvements",
        "false_blocks",
        "false_clears",
    ):
        assert getattr(serial.metrics, field) == getattr(parallel.metrics, field)


def test_not_evaluable_data_cannot_be_promoted() -> None:
    try:
        FunnelDecision(
            stage=FunnelStage.L1,
            status=StageStatus.PASS,
            continue_pipeline=True,
            mechanics_status=MechanicsStatus.PASS,
            data_eligible=True,
            scientific_outcome=ScientificOutcome.NOT_EVALUABLE_DATA,
        )
    except ValueError as exc:
        assert "missing eligible truth" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("NOT_EVALUABLE_DATA incorrectly received promotion")
