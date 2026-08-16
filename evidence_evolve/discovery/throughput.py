"""Work-conserving candidate execution with a staged development funnel.

This module is deliberately additive.  Locked generation-synchronous campaigns keep
their original runner.  The engine here owns execution order only: callbacks remain
the authorities for proposals, implementations, deterministic gates, receipts, and
archive admission.
"""

from __future__ import annotations

import time
import os
import subprocess
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evidence_evolve.models import (
    MechanicsStatus,
    ScientificOutcome,
    StrictModel,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FunnelStage(StrEnum):
    PROPOSE = "PROPOSE"
    IMPLEMENT = "IMPLEMENT"
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class StageStatus(StrEnum):
    PASS = "PASS"
    BLOCK = "BLOCK"


class CandidateTicket(StrictModel):
    """Frozen scheduling identity for one candidate attempt."""

    candidate_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    dispatch_index: int = Field(ge=1)
    lineage_id: str = Field(min_length=1)
    operator_class: str = Field(min_length=1)
    genetic_parent_id: str = Field(min_length=1)
    requires_structural_transition: bool = False


class FunnelDecision(StrictModel):
    """A frozen stage decision; prose and scores cannot override hard controls."""

    stage: Literal[FunnelStage.L0, FunnelStage.L1, FunnelStage.L2]
    status: StageStatus
    continue_pipeline: bool = False
    mechanics_status: MechanicsStatus
    data_eligible: bool
    controls: dict[str, bool] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    scientific_outcome: ScientificOutcome
    reason_codes: list[str] = Field(default_factory=list)
    admission_eligible: bool = False
    promotion_worthy: bool = False
    structural_transition_pass: bool = False
    structural_root_key: str | None = Field(default=None, min_length=1)
    incumbent_improved: bool = False

    @model_validator(mode="after")
    def hard_boundaries_are_consistent(self) -> "FunnelDecision":
        if self.status is StageStatus.BLOCK and self.continue_pipeline:
            raise ValueError("a blocked stage cannot continue the normal funnel")
        if self.stage is FunnelStage.L2 and self.continue_pipeline:
            raise ValueError("L2 is terminal")
        if self.mechanics_status is MechanicsStatus.FAIL and (
            self.scientific_outcome
            is not ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
        ):
            raise ValueError("failed mechanics must remain INVALID_MECHANICS_OR_ADAPTER")
        if self.scientific_outcome is ScientificOutcome.NOT_EVALUABLE_DATA and (
            self.data_eligible or self.admission_eligible or self.promotion_worthy
        ):
            raise ValueError("missing eligible truth cannot grant promotion or admission")
        if self.stage is not FunnelStage.L2 and (
            self.admission_eligible
            or self.promotion_worthy
            or self.structural_transition_pass
            or self.structural_root_key is not None
            or self.incumbent_improved
        ):
            raise ValueError("L0/L1 cannot grant L2 scheduling facts")
        if self.structural_transition_pass != (self.structural_root_key is not None):
            raise ValueError(
                "structural transition pass and structural root key must agree"
            )
        if self.admission_eligible and (
            self.status is not StageStatus.PASS
            or self.mechanics_status is not MechanicsStatus.PASS
            or not self.data_eligible
            or not self.controls
            or not all(self.controls.values())
        ):
            raise ValueError("admission eligibility requires every hard control")
        if self.incumbent_improved and (
            self.status is not StageStatus.PASS
            or self.mechanics_status is not MechanicsStatus.PASS
            or not self.data_eligible
            or not self.controls
            or not all(self.controls.values())
        ):
            raise ValueError("an incumbent refresh requires every hard control")
        return self


class ThroughputPolicy(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str = Field(min_length=1)
    total_candidate_budget: int = Field(ge=1)
    propose_workers: int = Field(default=8, ge=1, le=128)
    implement_workers: int = Field(default=8, ge=1, le=128)
    l0_workers: int = Field(default=8, ge=1, le=128)
    l1_workers: int = Field(default=4, ge=1, le=128)
    l2_workers: int = Field(default=2, ge=1, le=128)
    max_inflight_per_lineage: int = Field(default=2, ge=1)
    max_candidates_per_lineage: int | None = Field(default=None, ge=1)
    operator_quotas: dict[str, int] = Field(default_factory=dict)
    shadow_audit_stride: int | None = Field(default=None, ge=2)
    blind_artifacts_permitted: Literal[False] = False
    confirmation_permitted: Literal[False] = False

    @model_validator(mode="after")
    def quotas_fit_budget(self) -> "ThroughputPolicy":
        if any(not name or quota <= 0 for name, quota in self.operator_quotas.items()):
            raise ValueError("operator quotas require non-empty names and positive limits")
        if self.operator_quotas and (
            sum(self.operator_quotas.values()) != self.total_candidate_budget
        ):
            raise ValueError("operator quotas must exactly allocate the candidate budget")
        return self

    def workers(self, stage: FunnelStage) -> int:
        return {
            FunnelStage.PROPOSE: self.propose_workers,
            FunnelStage.IMPLEMENT: self.implement_workers,
            FunnelStage.L0: self.l0_workers,
            FunnelStage.L1: self.l1_workers,
            FunnelStage.L2: self.l2_workers,
        }[stage]


class ThroughputEvent(StrictModel):
    sequence: int = Field(ge=1)
    candidate_id: str
    stage: FunnelStage
    event: Literal["STARTED", "COMPLETED", "BLOCKED", "FAILED"]
    created_at: str
    elapsed_seconds: float | None = Field(default=None, ge=0.0)
    reason_codes: list[str] = Field(default_factory=list)


class CandidateFunnelRecord(StrictModel):
    ticket: CandidateTicket
    decisions: list[FunnelDecision] = Field(default_factory=list)
    terminal_stage: FunnelStage
    terminal_status: Literal["COMPLETE", "BLOCKED", "FAILED"]
    shadow_audit: bool = False
    admitted: bool = False
    error_type: str | None = None
    error: str | None = None
    wall_seconds: float = Field(ge=0.0)

    def decision(self, stage: FunnelStage) -> FunnelDecision | None:
        return next((item for item in self.decisions if item.stage is stage), None)


class ThroughputMetrics(StrictModel):
    wall_seconds: float = Field(ge=0.0)
    proposed: int = Field(ge=0)
    implemented: int = Field(ge=0)
    l0_completed: int = Field(ge=0)
    l1_completed: int = Field(ge=0)
    l2_completed: int = Field(ge=0)
    admitted: int = Field(ge=0)
    l2_dev_valid: int = Field(ge=0)
    structural_roots: int = Field(ge=0)
    useful_candidates: int = Field(ge=0)
    incumbent_improvements: int = Field(ge=0)
    shadow_audits: int = Field(ge=0)
    false_blocks: int = Field(ge=0)
    false_clears: int = Field(ge=0)
    candidates_per_wall_hour: float = Field(ge=0.0)
    dev_valid_per_wall_hour: float = Field(ge=0.0)
    structural_roots_per_wall_hour: float = Field(ge=0.0)
    useful_search_throughput: float = Field(ge=0.0)
    incumbent_improvements_per_wall_hour: float = Field(ge=0.0)


class ThroughputRunResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str
    records: list[CandidateFunnelRecord]
    events: list[ThroughputEvent]
    metrics: ThroughputMetrics
    resources: dict[str, Any]
    blind_artifacts_read: Literal[False] = False
    confirmation_runs: Literal[0] = 0
    scientific_authority: Literal["NONE_SCHEDULING_ONLY"] = (
        "NONE_SCHEDULING_ONLY"
    )


ProposeCallback = Callable[[CandidateTicket], Any]
ImplementCallback = Callable[[CandidateTicket, Any], Any]
L0Callback = Callable[[CandidateTicket, Any], FunnelDecision]
L1Callback = Callable[[CandidateTicket, Any, FunnelDecision], FunnelDecision]
L2Callback = Callable[[CandidateTicket, Any, FunnelDecision], FunnelDecision]
AdmitCallback = Callable[[CandidateFunnelRecord], None]
ReplenishCallback = Callable[[CandidateFunnelRecord], list[CandidateTicket]]


@dataclass(frozen=True)
class FunnelCallbacks:
    propose: ProposeCallback
    implement: ImplementCallback
    l0: L0Callback
    l1: L1Callback
    l2: L2Callback
    admit: AdmitCallback | None = None
    replenish: ReplenishCallback | None = None


@dataclass
class _Runtime:
    ticket: CandidateTicket
    queued_at: float
    started_at: float | None = None
    proposal: Any = None
    implementation: Any = None
    decisions: list[FunnelDecision] = field(default_factory=list)
    shadow_audit: bool = False
    record: CandidateFunnelRecord | None = None


class AsyncFunnelEngine:
    """Execute independent stages continuously while one thread owns transitions."""

    def __init__(self, policy: ThroughputPolicy, callbacks: FunnelCallbacks) -> None:
        self.policy = policy
        self.callbacks = callbacks
        self._events: list[ThroughputEvent] = []
        self._sequence = 0
        self._runtimes: dict[str, _Runtime] = {}
        self._operator_counts: Counter[str] = Counter()
        self._lineage_counts: Counter[str] = Counter()
        self._queues: dict[FunnelStage, deque[str]] = {
            stage: deque() for stage in FunnelStage
        }

    def _event(
        self,
        candidate_id: str,
        stage: FunnelStage,
        event: Literal["STARTED", "COMPLETED", "BLOCKED", "FAILED"],
        *,
        elapsed_seconds: float | None = None,
        reason_codes: list[str] | None = None,
    ) -> None:
        self._sequence += 1
        self._events.append(
            ThroughputEvent(
                sequence=self._sequence,
                candidate_id=candidate_id,
                stage=stage,
                event=event,
                created_at=_utc_now(),
                elapsed_seconds=elapsed_seconds,
                reason_codes=reason_codes or [],
            )
        )

    def _register(self, ticket: CandidateTicket) -> None:
        if ticket.candidate_id in self._runtimes:
            raise ValueError(f"duplicate candidate id: {ticket.candidate_id}")
        if len(self._runtimes) >= self.policy.total_candidate_budget:
            raise ValueError("replenishment exceeds the frozen candidate budget")
        quota = self.policy.operator_quotas.get(ticket.operator_class)
        if self.policy.operator_quotas and quota is None:
            raise ValueError(f"operator is absent from frozen quotas: {ticket.operator_class}")
        if quota is not None and self._operator_counts[ticket.operator_class] >= quota:
            raise ValueError(f"operator quota exceeded: {ticket.operator_class}")
        lineage_limit = self.policy.max_candidates_per_lineage
        if (
            lineage_limit is not None
            and self._lineage_counts[ticket.lineage_id] >= lineage_limit
        ):
            raise ValueError(f"lineage candidate quota exceeded: {ticket.lineage_id}")
        if any(
            item.ticket.dispatch_index == ticket.dispatch_index
            for item in self._runtimes.values()
        ):
            raise ValueError(f"duplicate dispatch index: {ticket.dispatch_index}")
        now = time.monotonic()
        self._runtimes[ticket.candidate_id] = _Runtime(ticket=ticket, queued_at=now)
        self._operator_counts[ticket.operator_class] += 1
        self._lineage_counts[ticket.lineage_id] += 1
        self._queues[FunnelStage.PROPOSE].append(ticket.candidate_id)

    def _lineage_inflight(self, lineage_id: str) -> int:
        return sum(
            runtime.started_at is not None
            and runtime.record is None
            and runtime.ticket.lineage_id == lineage_id
            for runtime in self._runtimes.values()
        )

    def _submit(
        self,
        stage: FunnelStage,
        runtime: _Runtime,
        executor: ThreadPoolExecutor,
    ) -> Future[Any]:
        ticket = runtime.ticket
        if runtime.started_at is None:
            runtime.started_at = time.monotonic()
        self._event(ticket.candidate_id, stage, "STARTED")
        if stage is FunnelStage.PROPOSE:
            return executor.submit(self.callbacks.propose, ticket)
        if stage is FunnelStage.IMPLEMENT:
            return executor.submit(self.callbacks.implement, ticket, runtime.proposal)
        if stage is FunnelStage.L0:
            return executor.submit(self.callbacks.l0, ticket, runtime.implementation)
        prior = runtime.decisions[-1]
        if stage is FunnelStage.L1:
            return executor.submit(
                self.callbacks.l1, ticket, runtime.implementation, prior
            )
        return executor.submit(
            self.callbacks.l2, ticket, runtime.implementation, prior
        )

    def _finish_record(
        self,
        runtime: _Runtime,
        stage: FunnelStage,
        status: Literal["COMPLETE", "BLOCKED", "FAILED"],
        *,
        error: Exception | None = None,
    ) -> CandidateFunnelRecord:
        started = runtime.started_at or runtime.queued_at
        record = CandidateFunnelRecord(
            ticket=runtime.ticket,
            decisions=list(runtime.decisions),
            terminal_stage=stage,
            terminal_status=status,
            shadow_audit=runtime.shadow_audit,
            error_type=type(error).__name__ if error is not None else None,
            error=str(error) if error is not None else None,
            wall_seconds=max(0.0, time.monotonic() - started),
        )
        runtime.record = record
        return record

    def _is_shadow_audit(self, ticket: CandidateTicket) -> bool:
        stride = self.policy.shadow_audit_stride
        return stride is not None and ticket.dispatch_index % stride == 0

    @staticmethod
    def _shadow_audit_eligible(decision: FunnelDecision) -> bool:
        return bool(
            decision.stage is FunnelStage.L1
            and decision.status is StageStatus.BLOCK
            and decision.mechanics_status is MechanicsStatus.PASS
            and decision.data_eligible
            and decision.controls
            and all(decision.controls.values())
            and decision.scientific_outcome is ScientificOutcome.VALID_NEGATIVE
        )

    @staticmethod
    def _dev_valid(decision: FunnelDecision | None) -> bool:
        return bool(
            decision
            and decision.stage is FunnelStage.L2
            and decision.mechanics_status is MechanicsStatus.PASS
            and decision.data_eligible
            and decision.controls
            and all(decision.controls.values())
            and decision.scientific_outcome
            not in {
                ScientificOutcome.NOT_EVALUABLE_DATA,
                ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
            }
        )

    def _metrics(self, wall_seconds: float) -> ThroughputMetrics:
        records = [
            runtime.record
            for runtime in self._runtimes.values()
            if runtime.record is not None
        ]
        l2_decisions = [record.decision(FunnelStage.L2) for record in records]
        dev_valid = sum(self._dev_valid(item) for item in l2_decisions)
        structural_keys = {
            item.structural_root_key
            for item in l2_decisions
            if item is not None and item.structural_transition_pass
        }
        useful_keys = {
            item.structural_root_key
            for item in l2_decisions
            if item is not None
            and self._dev_valid(item)
            and item.structural_transition_pass
        }
        structural = len(structural_keys)
        useful = len(useful_keys)
        false_blocks = 0
        false_clears = 0
        for record in records:
            l1 = record.decision(FunnelStage.L1)
            l2 = record.decision(FunnelStage.L2)
            if l1 is None or l2 is None:
                continue
            if record.shadow_audit and l1.status is StageStatus.BLOCK:
                false_blocks += int(l2.promotion_worthy)
            elif l1.status is StageStatus.PASS:
                false_clears += int(not l2.promotion_worthy)
        hours = wall_seconds / 3600.0

        def rate(count: int) -> float:
            return count / hours if hours > 0 else 0.0

        l2_completed = sum(item is not None for item in l2_decisions)
        return ThroughputMetrics(
            wall_seconds=wall_seconds,
            proposed=sum(runtime.proposal is not None for runtime in self._runtimes.values()),
            implemented=sum(
                runtime.implementation is not None for runtime in self._runtimes.values()
            ),
            l0_completed=sum(
                record.decision(FunnelStage.L0) is not None for record in records
            ),
            l1_completed=sum(
                record.decision(FunnelStage.L1) is not None for record in records
            ),
            l2_completed=l2_completed,
            admitted=sum(record.admitted for record in records),
            l2_dev_valid=dev_valid,
            structural_roots=structural,
            useful_candidates=useful,
            incumbent_improvements=sum(
                bool(item and item.incumbent_improved) for item in l2_decisions
            ),
            shadow_audits=sum(record.shadow_audit for record in records),
            false_blocks=false_blocks,
            false_clears=false_clears,
            candidates_per_wall_hour=rate(l2_completed),
            dev_valid_per_wall_hour=rate(dev_valid),
            structural_roots_per_wall_hour=rate(structural),
            useful_search_throughput=rate(useful),
            incumbent_improvements_per_wall_hour=rate(
                sum(bool(item and item.incumbent_improved) for item in l2_decisions)
            ),
        )

    def run(self, tickets: list[CandidateTicket]) -> ThroughputRunResult:
        if not tickets:
            raise ValueError("async funnel requires at least one candidate ticket")
        for ticket in sorted(tickets, key=lambda item: item.dispatch_index):
            self._register(ticket)

        started = time.monotonic()
        cpu_started = os.times()
        executors = {
            stage: ThreadPoolExecutor(
                max_workers=self.policy.workers(stage),
                thread_name_prefix=f"ee-{stage.value.lower()}",
            )
            for stage in FunnelStage
        }
        futures: dict[Future[Any], tuple[FunnelStage, str, float]] = {}
        try:
            while futures or any(self._queues.values()):
                for stage in reversed(list(FunnelStage)):
                    available = self.policy.workers(stage) - sum(
                        active_stage is stage
                        for active_stage, _candidate_id, _started in futures.values()
                    )
                    attempts = len(self._queues[stage])
                    while available > 0 and attempts > 0 and self._queues[stage]:
                        attempts -= 1
                        candidate_id = self._queues[stage].popleft()
                        runtime = self._runtimes[candidate_id]
                        if (
                            stage is FunnelStage.PROPOSE
                            and self._lineage_inflight(runtime.ticket.lineage_id)
                            >= self.policy.max_inflight_per_lineage
                        ):
                            self._queues[stage].append(candidate_id)
                            continue
                        future = self._submit(stage, runtime, executors[stage])
                        futures[future] = (stage, candidate_id, time.monotonic())
                        available -= 1

                if not futures:
                    raise RuntimeError("async funnel deadlocked with queued candidates")
                done, _pending = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    stage, candidate_id, stage_started = futures.pop(future)
                    runtime = self._runtimes[candidate_id]
                    elapsed = max(0.0, time.monotonic() - stage_started)
                    try:
                        value = future.result()
                    except Exception as exc:
                        self._event(
                            candidate_id,
                            stage,
                            "FAILED",
                            elapsed_seconds=elapsed,
                            reason_codes=[type(exc).__name__],
                        )
                        self._finish_record(runtime, stage, "FAILED", error=exc)
                        continue

                    if stage is FunnelStage.PROPOSE:
                        runtime.proposal = value
                        self._event(candidate_id, stage, "COMPLETED", elapsed_seconds=elapsed)
                        self._queues[FunnelStage.IMPLEMENT].append(candidate_id)
                        continue
                    if stage is FunnelStage.IMPLEMENT:
                        runtime.implementation = value
                        self._event(candidate_id, stage, "COMPLETED", elapsed_seconds=elapsed)
                        self._queues[FunnelStage.L0].append(candidate_id)
                        continue
                    if not isinstance(value, FunnelDecision) or value.stage is not stage:
                        raise TypeError(
                            f"{stage.value} callback returned an invalid stage decision"
                        )
                    runtime.decisions.append(value)
                    event = "COMPLETED" if value.status is StageStatus.PASS else "BLOCKED"
                    self._event(
                        candidate_id,
                        stage,
                        event,
                        elapsed_seconds=elapsed,
                        reason_codes=value.reason_codes,
                    )
                    if stage is FunnelStage.L0 and value.continue_pipeline:
                        self._queues[FunnelStage.L1].append(candidate_id)
                        continue
                    if stage is FunnelStage.L1:
                        if value.continue_pipeline:
                            self._queues[FunnelStage.L2].append(candidate_id)
                            continue
                        if self._is_shadow_audit(
                            runtime.ticket
                        ) and self._shadow_audit_eligible(value):
                            runtime.shadow_audit = True
                            self._queues[FunnelStage.L2].append(candidate_id)
                            continue
                    if stage is not FunnelStage.L2:
                        self._finish_record(runtime, stage, "BLOCKED")
                        continue

                    record = self._finish_record(runtime, stage, "COMPLETE")
                    if value.admission_eligible and not runtime.shadow_audit:
                        if self.callbacks.admit is not None:
                            self.callbacks.admit(record)
                        record.admitted = True
                    if self.callbacks.replenish is not None:
                        for ticket in self.callbacks.replenish(record):
                            self._register(ticket)
        finally:
            for executor in executors.values():
                executor.shutdown(wait=True, cancel_futures=False)

        wall_seconds = max(0.0, time.monotonic() - started)
        cpu_finished = os.times()
        cpu_seconds = sum(
            (
                cpu_finished.user - cpu_started.user,
                cpu_finished.system - cpu_started.system,
                cpu_finished.children_user - cpu_started.children_user,
                cpu_finished.children_system - cpu_started.children_system,
            )
        )
        records = [
            runtime.record
            for runtime in sorted(
                self._runtimes.values(), key=lambda item: item.ticket.dispatch_index
            )
            if runtime.record is not None
        ]
        return ThroughputRunResult(
            policy_id=self.policy.policy_id,
            records=records,
            events=self._events,
            metrics=self._metrics(wall_seconds),
            resources=_resource_snapshot(
                self.policy,
                wall_seconds=wall_seconds,
                cpu_seconds=max(0.0, cpu_seconds),
            ),
        )


@lru_cache(maxsize=1)
def _hardware_resource_snapshot() -> dict[str, Any]:
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    cpu_quota = None
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpu_quota = max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    memory_limit = None
    try:
        raw_memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw_memory != "max":
            memory_limit = int(raw_memory)
    except (OSError, ValueError):
        pass
    try:
        gpu = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        visible_gpus = [line.strip() for line in gpu.stdout.splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        visible_gpus = []
    return {
        "cpu_affinity_count": len(affinity),
        "cgroup_cpu_quota_count": cpu_quota,
        "cgroup_memory_limit_bytes": memory_limit,
        "visible_gpus": visible_gpus,
    }


def _resource_snapshot(
    policy: ThroughputPolicy,
    *,
    wall_seconds: float,
    cpu_seconds: float,
) -> dict[str, Any]:
    hardware = _hardware_resource_snapshot()
    allocated_cores = hardware.get("cgroup_cpu_quota_count") or hardware.get(
        "cpu_affinity_count"
    )
    allocated_cpu_utilization = (
        100.0 * cpu_seconds / (wall_seconds * float(allocated_cores))
        if wall_seconds > 0.0 and allocated_cores
        else 0.0
    )
    return {
        **hardware,
        "process_tree_cpu_seconds": cpu_seconds,
        "allocated_cpu_utilization_percent": allocated_cpu_utilization,
        "stage_workers": {
            stage.value: policy.workers(stage) for stage in FunnelStage
        },
        "max_inflight_per_lineage": policy.max_inflight_per_lineage,
    }


__all__ = [
    "AsyncFunnelEngine",
    "CandidateFunnelRecord",
    "CandidateTicket",
    "FunnelCallbacks",
    "FunnelDecision",
    "FunnelStage",
    "StageStatus",
    "ThroughputMetrics",
    "ThroughputPolicy",
    "ThroughputRunResult",
]
