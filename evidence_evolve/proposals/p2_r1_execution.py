from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import atomic_write_json, create_once_bytes, create_once_json
from evidence_evolve.hashing import sha256_bytes, sha256_file, sha256_object
from evidence_evolve.models import StrictModel
from evidence_evolve.proposals.non_inferiority import (
    Arm,
    P2R1Protocol,
    load_and_validate_p2_r1_protocol,
)
from evidence_evolve.proposals.parity_analysis import (
    ArmRun,
    P2R1AnalysisInput,
    ProposalSlot,
    _terminal_class,
    analyze_p2_r1,
)
from evidence_evolve.proposals.p2_r1_transport import TransportLedgerRecord


RUN_STATUS = Literal["PLANNED", "RUNNING", "INTERRUPTED", "COMPLETE"]
EXECUTION_MODE = Literal["ZERO_CALL_E2E", "REMOTE_SMOKE", "FORMAL"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class P2R1SlotSpec(StrictModel):
    slot_id: str
    slot: int = Field(ge=1, le=5)
    block: int = Field(ge=1, le=10)
    arm: Arm
    paired_local_seed: int
    model: Literal["gpt-5.6-terra"]


class P2R1RunSpec(StrictModel):
    run_id: str
    sequence: int = Field(ge=1, le=20)
    block: int = Field(ge=1, le=10)
    position_in_block: int = Field(ge=1, le=2)
    arm: Arm
    paired_local_seed: int
    state_namespace: str
    results_dir: str
    database_path: str
    audit_dir: str
    slots: Annotated[list[P2R1SlotSpec], Field(min_length=5, max_length=5)]

    @model_validator(mode="after")
    def identity_is_canonical(self) -> "P2R1RunSpec":
        expected_run = f"p2-r1-b{self.block:02d}-{self.arm}"
        expected_namespace = f"p2-r1-block-{self.block:02d}-{self.arm}"
        if self.run_id != expected_run or self.state_namespace != expected_namespace:
            raise ValueError("run or state namespace is not protocol-derived")
        for slot in self.slots:
            if (slot.block, slot.arm, slot.paired_local_seed) != (
                self.block,
                self.arm,
                self.paired_local_seed,
            ):
                raise ValueError("slot identity diverges from its run")
        if [slot.slot for slot in self.slots] != [1, 2, 3, 4, 5]:
            raise ValueError("each run must contain the five frozen slots")
        return self


class P2R1Schedule(StrictModel):
    protocol_id: Literal["SHINKA_NATIVE_P2_R1"]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runs: Annotated[list[P2R1RunSpec], Field(min_length=20, max_length=20)]

    @model_validator(mode="after")
    def schedule_is_disjoint(self) -> "P2R1Schedule":
        unique_fields = {
            "run_id": [run.run_id for run in self.runs],
            "sequence": [run.sequence for run in self.runs],
            "state_namespace": [run.state_namespace for run in self.runs],
            "results_dir": [str(Path(run.results_dir).resolve()) for run in self.runs],
            "database_path": [str(Path(run.database_path).resolve()) for run in self.runs],
            "audit_dir": [str(Path(run.audit_dir).resolve()) for run in self.runs],
        }
        for label, values in unique_fields.items():
            if len(set(values)) != len(values):
                raise ValueError(f"P2-R1 schedule has a {label} collision")
        if [run.sequence for run in self.runs] != list(range(1, 21)):
            raise ValueError("P2-R1 sequence must be exactly 1 through 20")
        pairs = {(run.block, run.arm) for run in self.runs}
        if pairs != {
            (block, arm)
            for block in range(1, 11)
            for arm in ("official", "native")
        }:
            raise ValueError("P2-R1 schedule must contain every block/arm exactly once")
        return self


class P2R1StartManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Literal["SHINKA_NATIVE_P2_R1"]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    executor_parent_lineage: str = Field(pattern=r"^[0-9a-f]{40}$")
    created_at: str
    execution_mode: EXECUTION_MODE
    dry_run: bool
    remote_calls_permitted: bool
    transport_mode: Literal["LOCAL_DETERMINISTIC", "REMOTE"]
    remote_slot_budget: Literal[0, 2, 100]
    slot_budget_per_run: Literal[1, 5]
    authorized_run_ids: list[str]
    gate_receipt_hashes: dict[str, str]
    schedule_source: Literal["protocol.design.schedule"]
    schedule: P2R1Schedule
    frozen_asset_hashes: dict[str, str]
    request_metadata: dict[str, Any]
    baseline_admission: dict[str, Any]
    provider_admission: dict[str, Any]
    resources: dict[str, Any]
    scientific_slots_total: Literal[100] = 100
    transport_attempts_per_slot_max: Literal[3] = 3
    scientific_resampling_permitted: Literal[False] = False

    @model_validator(mode="after")
    def dry_run_cannot_permit_remote_calls(self) -> "P2R1StartManifest":
        run_count = len(self.authorized_run_ids)
        expected = {
            "ZERO_CALL_E2E": (True, False, "LOCAL_DETERMINISTIC", 0, 5, 20),
            "REMOTE_SMOKE": (False, True, "REMOTE", 2, 1, 2),
            "FORMAL": (False, True, "REMOTE", 100, 5, 20),
        }[self.execution_mode]
        actual = (
            self.dry_run,
            self.remote_calls_permitted,
            self.transport_mode,
            self.remote_slot_budget,
            self.slot_budget_per_run,
            run_count,
        )
        if actual != expected or len(set(self.authorized_run_ids)) != run_count:
            raise ValueError("execution mode and remote budget authority are inconsistent")
        if self.execution_mode == "FORMAL":
            if set(self.gate_receipt_hashes) != {"zero_call_e2e", "remote_smoke"}:
                raise ValueError("formal execution lacks both admission gate receipts")
        elif self.execution_mode == "REMOTE_SMOKE":
            if set(self.gate_receipt_hashes) != {"zero_call_e2e"}:
                raise ValueError("remote smoke lacks zero-call E2E admission")
        elif self.gate_receipt_hashes:
            raise ValueError("pre-formal execution cannot carry a budget unlock")
        return self


class P2R1SmokeRunSpec(StrictModel):
    run_id: str
    block: int = Field(ge=1, le=10)
    arm: Arm
    paired_local_seed: int
    state_namespace: str
    results_dir: str
    database_path: str
    audit_dir: str

    @model_validator(mode="after")
    def smoke_is_visibly_non_scientific(self) -> "P2R1SmokeRunSpec":
        suffix = f"-{self.arm}"
        if not self.run_id.startswith("p2-r1-smoke-") or not self.run_id.endswith(
            suffix
        ):
            raise ValueError("smoke run identity is not canonical")
        if self.state_namespace != self.run_id:
            raise ValueError("smoke state namespace must equal its isolated run id")
        return self


class P2R1ZeroCallE2EReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Literal["SHINKA_NATIVE_P2_R1"]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: Literal["PASS"] = "PASS"
    remote_model_calls: Literal[0] = 0
    production_path: Literal[
        "DRIVER_ENGINE_STATE_PROMPT_RECEIPT_COLLECTOR_FROZEN_ANALYZER_FINAL_ASSESSMENT"
    ]
    run_count: Literal[20] = 20
    slot_count: Literal[100] = 100
    start_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scientific_outcome_authority: Literal["NONE"] = "NONE"


class P2R1SmokeArmReceipt(StrictModel):
    arm: Arm
    run_id: str
    state_namespace: str
    scientific_slots: Literal[1] = 1
    transport_attempts: int = Field(ge=1, le=3)
    request_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_reached: Literal[True] = True
    terminal_funnel_state: str


class P2R1RemoteSmokeReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Literal["SHINKA_NATIVE_P2_R1"]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: Literal["PASS"] = "PASS"
    zero_call_e2e_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    remote_scientific_slots: Literal[2] = 2
    slots_per_arm: Literal[1] = 1
    start_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arms: Annotated[list[P2R1SmokeArmReceipt], Field(min_length=2, max_length=2)]
    reusable_as_scientific_evidence: Literal[False] = False
    scientific_outcome_authority: Literal["NONE"] = "NONE"

    @model_validator(mode="after")
    def arms_are_isolated(self) -> "P2R1RemoteSmokeReceipt":
        if {item.arm for item in self.arms} != {"official", "native"} or len(
            {item.state_namespace for item in self.arms}
        ) != 2:
            raise ValueError("smoke must contain two isolated arms")
        return self


class P2R1RunManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    start_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run: P2R1RunSpec
    command: list[str]
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_asset_hashes: dict[str, str]


class P2R1ScientificSlotReceipt(StrictModel):
    slot_id: str
    block: int = Field(ge=1, le=10)
    arm: Arm
    slot: int = Field(ge=1, le=5)
    paired_local_seed: int
    model: Literal["gpt-5.6-terra"]
    rendered_system_prompt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    rendered_user_prompt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    request_payload_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    transport_attempt_count: int = Field(ge=0, le=3)
    transport_attempt_payload_sha256s: list[str] = Field(max_length=3)
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
    ]
    state_namespace: str
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class P2R1RunReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: str
    block: int
    arm: Arm
    paired_local_seed: int
    state_namespace: str
    run_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int
    started_at: str
    finished_at: str
    wall_seconds: float = Field(ge=0)
    transport_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_run: ArmRun
    scientific_slots: Annotated[
        list[P2R1ScientificSlotReceipt], Field(min_length=5, max_length=5)
    ]
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resume_semantics: Literal[
        "RECEIPT_AND_DATABASE_DETERMINISTIC_NO_COMPLETED_SLOT_RERUN"
    ] = "RECEIPT_AND_DATABASE_DETERMINISTIC_NO_COMPLETED_SLOT_RERUN"

    @model_validator(mode="after")
    def slot_receipts_match_analysis_run(self) -> "P2R1RunReceipt":
        if (self.block, self.arm, self.state_namespace) != (
            self.analysis_run.block,
            self.analysis_run.arm,
            self.analysis_run.state_namespace,
        ):
            raise ValueError("run receipt identity differs from analysis input")
        for receipt, proposal in zip(self.scientific_slots, self.analysis_run.slots):
            if (
                receipt.block != self.block
                or receipt.arm != self.arm
                or receipt.slot != proposal.slot
                or receipt.state_namespace != self.state_namespace
                or receipt.protocol_sha256 != self.protocol_sha256
                or receipt.executor_commit != self.executor_commit
                or receipt.rendered_system_prompt_sha256
                != proposal.rendered_system_prompt_sha256
                or receipt.rendered_user_prompt_sha256
                != proposal.rendered_user_prompt_sha256
                or receipt.request_payload_sha256
                != proposal.request_payload_sha256
                or receipt.transport_attempt_payload_sha256s
                != proposal.transport_attempt_payload_sha256s
                or receipt.transport_attempt_count
                != len(proposal.transport_attempt_payload_sha256s)
                or receipt.terminal_funnel_state
                != _terminal_class(proposal, self.analysis_run.baseline_score)
            ):
                raise ValueError("scientific slot receipt differs from analysis input")
        return self


def build_p2_r1_schedule(
    protocol: P2R1Protocol, *, run_root: Path
) -> P2R1Schedule:
    """Derive all run and slot identities solely from the frozen protocol."""

    resolved_root = run_root.resolve()
    runs: list[P2R1RunSpec] = []
    sequence = 0
    for matched_block in protocol.design.schedule:
        for position, arm in enumerate(matched_block.order, start=1):
            sequence += 1
            results_dir = resolved_root / "results" / f"block_{matched_block.block:02d}" / arm
            audit_dir = resolved_root / "audit" / f"block_{matched_block.block:02d}" / arm
            run_id = f"p2-r1-b{matched_block.block:02d}-{arm}"
            slots = [
                P2R1SlotSpec(
                    slot_id=f"{run_id}-slot-{slot:02d}",
                    slot=slot,
                    block=matched_block.block,
                    arm=arm,
                    paired_local_seed=matched_block.local_seed,
                    model=protocol.provider.model,
                )
                for slot in range(1, protocol.design.proposal_slots_per_run + 1)
            ]
            runs.append(
                P2R1RunSpec(
                    run_id=run_id,
                    sequence=sequence,
                    block=matched_block.block,
                    position_in_block=position,
                    arm=arm,
                    paired_local_seed=matched_block.local_seed,
                    state_namespace=f"p2-r1-block-{matched_block.block:02d}-{arm}",
                    results_dir=str(results_dir),
                    database_path=str(results_dir / "programs.sqlite"),
                    audit_dir=str(audit_dir),
                    slots=slots,
                )
            )
    return P2R1Schedule(
        protocol_id=protocol.protocol_id,
        protocol_sha256=protocol.protocol_sha256,
        runs=runs,
    )


def _git_output(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _resource_receipt() -> dict[str, Any]:
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    memory_bytes = None
    try:
        memory_bytes = int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemTotal:")
            )
        ) * 1024
    except (OSError, StopIteration, ValueError):
        pass
    try:
        gpu = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        visible_gpus = [
            line.strip() for line in gpu.stdout.splitlines() if line.strip()
        ]
    except FileNotFoundError:
        visible_gpus = []
    cpu_quota = None
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpu_quota = max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    cgroup_memory = None
    try:
        raw_memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw_memory != "max":
            cgroup_memory = int(raw_memory)
    except (OSError, ValueError):
        pass
    return {
        "cpu_affinity": affinity,
        "cpu_affinity_count": len(affinity),
        "cgroup_cpu_quota_count": cpu_quota,
        "system_memory_bytes": memory_bytes,
        "cgroup_memory_limit_bytes": cgroup_memory,
        "visible_gpus": visible_gpus,
    }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class P2R1ExecutionDriver:
    def __init__(
        self,
        *,
        repo: Path,
        protocol_path: Path,
        run_root: Path,
        execution_mode: EXECUTION_MODE,
        max_parallel_blocks: int | None = None,
        zero_call_receipt: Path | None = None,
        remote_smoke_receipt: Path | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.protocol_path = protocol_path.resolve()
        self.run_root = run_root.resolve()
        self.execution_mode = execution_mode
        self.dry_run = execution_mode == "ZERO_CALL_E2E"
        self.max_parallel_blocks = max_parallel_blocks
        self.zero_call_receipt = (
            zero_call_receipt.resolve() if zero_call_receipt else None
        )
        self.remote_smoke_receipt = (
            remote_smoke_receipt.resolve() if remote_smoke_receipt else None
        )
        self.protocol = load_and_validate_p2_r1_protocol(
            self.protocol_path, repo=self.repo
        )
        self.schedule = build_p2_r1_schedule(self.protocol, run_root=self.run_root)
        self.executor_commit = _git_output(self.repo, "rev-parse", "HEAD")
        self.start_manifest_path = self.run_root / "start_manifest.json"
        self.task_dir = self.run_root / "frozen_task"

    def _smoke_runs(self) -> list[P2R1SmokeRunSpec]:
        first = self.protocol.design.schedule[0]
        runs = []
        for arm in ("official", "native"):
            run_id = f"p2-r1-smoke-{self.executor_commit[:12]}-{arm}"
            results = self.run_root / "results" / arm
            runs.append(
                P2R1SmokeRunSpec(
                    run_id=run_id,
                    block=first.block,
                    arm=arm,
                    paired_local_seed=first.local_seed,
                    state_namespace=run_id,
                    results_dir=str(results),
                    database_path=str(results / "programs.sqlite"),
                    audit_dir=str(self.run_root / "audit" / arm),
                )
            )
        return runs

    def _gate_receipt_hashes(self) -> dict[str, str]:
        if self.execution_mode == "ZERO_CALL_E2E":
            return {}
        if self.zero_call_receipt is None:
            raise RuntimeError(
                "remote budget is locked: a zero-call E2E receipt is required"
            )
        zero = P2R1ZeroCallE2EReceipt.model_validate_json(
            self.zero_call_receipt.read_text(encoding="utf-8")
        )
        expected = (
            self.protocol.protocol_id,
            self.protocol.protocol_sha256,
            self.executor_commit,
        )
        if (zero.protocol_id, zero.protocol_sha256, zero.executor_commit) != expected:
            raise RuntimeError("zero-call receipt does not bind this frozen executor")
        hashes = {"zero_call_e2e": sha256_file(self.zero_call_receipt)}
        if self.execution_mode == "REMOTE_SMOKE":
            return hashes
        if self.remote_smoke_receipt is None:
            raise RuntimeError(
                "formal budget is locked: a remote-smoke receipt is required"
            )
        smoke = P2R1RemoteSmokeReceipt.model_validate_json(
            self.remote_smoke_receipt.read_text(encoding="utf-8")
        )
        if (smoke.protocol_id, smoke.protocol_sha256, smoke.executor_commit) != expected:
            raise RuntimeError("remote-smoke receipt does not bind this frozen executor")
        if smoke.zero_call_e2e_receipt_sha256 != hashes["zero_call_e2e"]:
            raise RuntimeError("remote smoke is not bound to the zero-call receipt")
        hashes["remote_smoke"] = sha256_file(self.remote_smoke_receipt)
        return hashes

    def _assert_clean_checkout(self) -> None:
        if _git_output(self.repo, "status", "--porcelain"):
            raise RuntimeError("P2-R1 admission requires a clean checkout")
        protocol_commit = _git_output(
            self.repo,
            "log",
            "-1",
            "--format=%H",
            "--",
            str(self.protocol_path.relative_to(self.repo)),
        )
        if protocol_commit != self.executor_commit:
            raise RuntimeError(
                "P2-R1 must execute from the exact commit that froze the protocol"
            )

    def _prepare_task(self) -> dict[str, str]:
        sources = {
            "initial.py": self.repo / self.protocol.frozen_assets["initial_program"].path,
            "evaluate.py": self.repo / self.protocol.frozen_assets["evaluator"].path,
            "shinka.yaml": self.repo / self.protocol.frozen_assets["config"].path,
        }
        hashes: dict[str, str] = {}
        for name, source in sources.items():
            destination = self.task_dir / name
            payload = source.read_bytes()
            if destination.exists():
                if destination.read_bytes() != payload:
                    raise RuntimeError(f"frozen task asset changed: {destination}")
            else:
                create_once_bytes(destination, payload)
            hashes[name] = sha256_file(destination)
        return hashes

    def _claim_namespaces(self) -> None:
        claims = self.run_root / "state_namespaces"
        runs: list[P2R1RunSpec | P2R1SmokeRunSpec]
        runs = (
            self._smoke_runs()
            if self.execution_mode == "REMOTE_SMOKE"
            else self.schedule.runs
        )
        for run in runs:
            claim_path = claims / f"{sha256_object(run.state_namespace)}.json"
            payload = {
                "state_namespace": run.state_namespace,
                "run_id": run.run_id,
                "results_dir": run.results_dir,
                "database_path": run.database_path,
            }
            if claim_path.exists():
                if _load_json(claim_path) != payload:
                    raise RuntimeError("state namespace collision was detected")
            else:
                create_once_json(claim_path, payload)
            results_dir = Path(run.results_dir)
            if results_dir.is_symlink():
                raise RuntimeError("symlinked results namespaces are forbidden")
            results_dir.mkdir(parents=True, exist_ok=True)
            Path(run.audit_dir).mkdir(parents=True, exist_ok=True)

    def _baseline_admission(self) -> dict[str, Any]:
        admission_dir = self.run_root / "baseline_admission"
        receipt_path = admission_dir / "receipt.json"
        if receipt_path.exists():
            receipt = _load_json(receipt_path)
            if receipt["initial_program_sha256"] != sha256_file(
                self.task_dir / "initial.py"
            ) or receipt["evaluator_sha256"] != sha256_file(
                self.task_dir / "evaluate.py"
            ):
                raise RuntimeError("baseline admission assets changed on resume")
            return receipt
        results_dir = admission_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(self.task_dir / "evaluate.py"),
                "--program_path",
                str(self.task_dir / "initial.py"),
                "--results_dir",
                str(results_dir),
            ],
            cwd=self.repo,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"frozen baseline admission failed: exit={completed.returncode}"
            )
        correct = _load_json(results_dir / "correct.json")
        metrics = _load_json(results_dir / "metrics.json")
        if correct.get("correct") is not True:
            raise RuntimeError("frozen incumbent is evaluator-invalid at admission")
        receipt = {
            "initial_program_sha256": sha256_file(self.task_dir / "initial.py"),
            "evaluator_sha256": sha256_file(self.task_dir / "evaluate.py"),
            "baseline_score": float(metrics["combined_score"]),
            "correct_json_sha256": sha256_file(results_dir / "correct.json"),
            "metrics_json_sha256": sha256_file(results_dir / "metrics.json"),
            "remote_model_calls": 0,
        }
        create_once_json(receipt_path, receipt)
        return receipt

    def _provider_admission(self) -> dict[str, Any]:
        candidates = set()
        path_node = shutil.which("node")
        if path_node:
            candidates.add(Path(path_node).resolve())
        candidates.update(Path("/opt").glob("node-*-linux-x64/bin/node"))
        compatible: list[tuple[tuple[int, int, int], Path, str]] = []
        for candidate in candidates:
            completed = subprocess.run(
                [str(candidate), "--version"],
                check=False,
                capture_output=True,
                text=True,
            )
            match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", completed.stdout.strip())
            if completed.returncode == 0 and match and int(match.group(1)) >= 18:
                compatible.append(
                    (
                        tuple(int(match.group(index)) for index in (1, 2, 3)),
                        candidate.resolve(),
                        completed.stdout.strip(),
                    )
                )
        if not compatible:
            raise RuntimeError("P2-R1 requires an available Node.js >=18 runtime")
        _version_tuple, node_path, node_version = max(
            compatible, key=lambda item: (item[0], str(item[1]))
        )
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            [str(node_path.parent), environment.get("PATH", "")]
        )
        command = [*shlex.split(self.protocol.provider.headless_command), "--check"]
        completed = subprocess.run(
            command,
            cwd=self.repo,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        expected_codex = self.protocol.provider.codex_cli_version.removeprefix(
            "codex-cli "
        )
        codex_row = re.search(
            rf"\|\s*codex\s*\|\s*✓\s*\|\s*oauth\s*\|\s*{re.escape(expected_codex)}\s*\|",
            output,
        )
        if completed.returncode != 0 or codex_row is None:
            raise RuntimeError(
                "frozen headless/Codex transport is unavailable or has wrong auth/version"
            )
        return {
            "node_executable": str(node_path),
            "node_version": node_version,
            "headless_check_command": command,
            "headless_check_output_sha256": sha256_bytes(output.encode("utf-8")),
            "codex_cli_version": self.protocol.provider.codex_cli_version,
            "codex_auth": "oauth",
            "remote_model_calls": 0,
        }

    def _start_manifest(
        self,
        baseline_admission: dict[str, Any],
        provider_admission: dict[str, Any],
    ) -> P2R1StartManifest:
        resources = _resource_receipt()
        resources["max_parallel_blocks"] = self._parallelism(resources)
        smoke_runs = self._smoke_runs()
        authorized_run_ids = (
            [run.run_id for run in smoke_runs]
            if self.execution_mode == "REMOTE_SMOKE"
            else [run.run_id for run in self.schedule.runs]
        )
        return P2R1StartManifest(
            protocol_id=self.protocol.protocol_id,
            protocol_sha256=self.protocol.protocol_sha256,
            executor_commit=self.executor_commit,
            executor_parent_lineage=self.protocol.repository_base_commit,
            created_at=_utc_now(),
            execution_mode=self.execution_mode,
            dry_run=self.dry_run,
            remote_calls_permitted=self.execution_mode != "ZERO_CALL_E2E",
            transport_mode=(
                "LOCAL_DETERMINISTIC"
                if self.execution_mode == "ZERO_CALL_E2E"
                else "REMOTE"
            ),
            remote_slot_budget={
                "ZERO_CALL_E2E": 0,
                "REMOTE_SMOKE": 2,
                "FORMAL": 100,
            }[self.execution_mode],
            slot_budget_per_run=(1 if self.execution_mode == "REMOTE_SMOKE" else 5),
            authorized_run_ids=authorized_run_ids,
            gate_receipt_hashes=self._gate_receipt_hashes(),
            schedule_source="protocol.design.schedule",
            schedule=self.schedule,
            frozen_asset_hashes={
                key: binding.sha256
                for key, binding in self.protocol.frozen_assets.items()
            },
            request_metadata={
                "model": self.protocol.provider.model,
                "reasoning_effort": self.protocol.provider.reasoning_effort,
                "temperature": self.protocol.provider.temperature,
                "max_output_tokens": self.protocol.provider.max_output_tokens_per_call,
                "tool_access": self.protocol.provider.tool_access,
                "structured_output": self.protocol.provider.structured_output,
                "headless_command": self.protocol.provider.headless_command,
                "headless_timeout_seconds": (
                    self.protocol.provider.headless_timeout_seconds_per_attempt
                ),
                "dynamic_prompt_hashes": "RECORDED_BEFORE_EACH_TRANSPORT_ATTEMPT",
                "remote_invocation_in_zero_call_e2e": False,
            },
            baseline_admission=baseline_admission,
            provider_admission=provider_admission,
            resources=resources,
        )

    def _parallelism(self, resources: dict[str, Any]) -> int:
        requested = self.max_parallel_blocks
        if requested is None:
            available = int(resources.get("cpu_affinity_count") or 1)
            quota = resources.get("cgroup_cpu_quota_count")
            if quota is not None:
                available = min(available, int(quota))
            requested = min(10, max(1, available))
        if not 1 <= requested <= 10:
            raise ValueError("max_parallel_blocks must be between 1 and 10")
        return requested

    def admit(self) -> P2R1StartManifest:
        self._assert_clean_checkout()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._prepare_task()
        self._claim_namespaces()
        baseline_admission = self._baseline_admission()
        provider_admission = self._provider_admission()
        proposed = self._start_manifest(baseline_admission, provider_admission)
        if self.start_manifest_path.exists():
            existing = P2R1StartManifest.model_validate_json(
                self.start_manifest_path.read_text(encoding="utf-8")
            )
            comparable_existing = existing.model_dump(exclude={"created_at"})
            comparable_proposed = proposed.model_dump(exclude={"created_at"})
            if comparable_existing != comparable_proposed:
                raise RuntimeError("immutable start manifest does not match resume request")
            return existing
        create_once_json(self.start_manifest_path, proposed)
        return proposed

    def _command(
        self, run: P2R1RunSpec | P2R1SmokeRunSpec, *, proposal_slots: int = 5
    ) -> list[str]:
        generations = str(proposal_slots + 1)
        common = [
            "--task-dir",
            str(self.task_dir),
            "--config-fname",
            "shinka.yaml",
        ]
        if run.arm == "official":
            return [
                sys.executable,
                "-m",
                "evidence_evolve",
                "search",
                "shinka-official-materialized",
                "--",
                *common,
                "--results_dir",
                run.results_dir,
                "--num_generations",
                generations,
            ]
        return [
            sys.executable,
            "-m",
            "evidence_evolve",
            "search",
            "shinka-native",
            "--run-id",
            run.run_id,
            "--task-dir",
            str(self.task_dir),
            "--results-dir",
            run.results_dir,
            "--num-generations",
            generations,
            "--config-fname",
            "shinka.yaml",
            "--proposal-materializer",
            "EVIDENCE_EVOLVE_V1",
        ]

    def _run_manifest(self, run: P2R1RunSpec) -> tuple[Path, P2R1RunManifest]:
        command = self._command(run)
        manifest = P2R1RunManifest(
            protocol_sha256=self.protocol.protocol_sha256,
            executor_commit=self.executor_commit,
            start_manifest_sha256=sha256_file(self.start_manifest_path),
            run=run,
            command=command,
            command_sha256=sha256_object(command),
            task_asset_hashes={
                name: sha256_file(self.task_dir / name)
                for name in ("initial.py", "evaluate.py", "shinka.yaml")
            },
        )
        path = Path(run.audit_dir) / "run_manifest.json"
        if path.exists():
            existing = P2R1RunManifest.model_validate_json(path.read_text())
            if existing != manifest:
                raise RuntimeError(f"run manifest changed on resume: {run.run_id}")
        else:
            create_once_json(path, manifest)
        return path, manifest

    def _environment(self, run: P2R1RunSpec | P2R1SmokeRunSpec) -> dict[str, str]:
        environment = os.environ.copy()
        node_dir = str(
            Path(
                _load_json(self.start_manifest_path)["provider_admission"][
                    "node_executable"
                ]
            ).parent
        )
        environment["PATH"] = os.pathsep.join(
            [node_dir, environment.get("PATH", "")]
        )
        runtime_dir = self.repo / "research/parity/p2_r1_runtime"
        current_pythonpath = environment.get("PYTHONPATH")
        paths = [str(runtime_dir), str(self.repo)]
        if current_pythonpath:
            paths.append(current_pythonpath)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(paths),
                "EVIDENCE_EVOLVE_P2_R1_SEED": str(run.paired_local_seed),
                "EVIDENCE_EVOLVE_P2_R1_SEED_LAYER": str(
                    self.repo / self.protocol.frozen_assets["seed_layer"].path
                ),
                "EVIDENCE_EVOLVE_P2_R1_AUDIT_DIR": run.audit_dir,
                "EVIDENCE_EVOLVE_P2_R1_RUN_ID": run.run_id,
                "EVIDENCE_EVOLVE_P2_R1_BLOCK": str(run.block),
                "EVIDENCE_EVOLVE_P2_R1_ARM": run.arm,
                "EVIDENCE_EVOLVE_P2_R1_STATE_NAMESPACE": run.state_namespace,
                "EVIDENCE_EVOLVE_P2_R1_START_MANIFEST": str(
                    self.start_manifest_path
                ),
                "EVIDENCE_EVOLVE_P2_R1_START_MANIFEST_SHA256": sha256_file(
                    self.start_manifest_path
                ),
                "EVIDENCE_EVOLVE_P2_R1_PROTOCOL_SHA256": self.protocol.protocol_sha256,
                "EVIDENCE_EVOLVE_P2_R1_EXECUTOR_COMMIT": self.executor_commit,
                "EVIDENCE_EVOLVE_P2_R1_RESULTS_DIR": run.results_dir,
                "EVIDENCE_EVOLVE_P2_R1_MODEL": (
                    f"headless/codex@{self.protocol.provider.model}"
                    f"?effort={self.protocol.provider.reasoning_effort}"
                ),
                "EVIDENCE_EVOLVE_P2_R1_TRANSPORT_MODE": (
                    "LOCAL_DETERMINISTIC"
                    if self.execution_mode == "ZERO_CALL_E2E"
                    else "REMOTE"
                ),
                "EVIDENCE_EVOLVE_P2_R1_INITIAL_SHA256": (
                    self.protocol.frozen_assets["initial_program"].sha256
                ),
                "EVIDENCE_EVOLVE_P2_R1_BASELINE_SCORE": str(
                    _load_json(self.start_manifest_path)["baseline_admission"][
                        "baseline_score"
                    ]
                ),
                "SHINKA_HEADLESS_COMMAND": self.protocol.provider.headless_command,
                "SHINKA_HEADLESS_TIMEOUT": str(
                    self.protocol.provider.headless_timeout_seconds_per_attempt
                ),
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            }
        )
        return environment

    def _status_path(self, run: P2R1RunSpec) -> Path:
        return Path(run.audit_dir) / "run_state.json"

    def _write_status(self, run: P2R1RunSpec, status: RUN_STATUS, **extra: Any) -> None:
        atomic_write_json(
            self._status_path(run),
            {
                "run_id": run.run_id,
                "status": status,
                "updated_at": _utc_now(),
                **extra,
            },
        )

    def _receipt_path(self, run: P2R1RunSpec) -> Path:
        return Path(run.audit_dir) / "run_receipt.json"

    def _execute_one(self, run: P2R1RunSpec) -> P2R1RunReceipt:
        receipt_path = self._receipt_path(run)
        if receipt_path.exists():
            return P2R1RunReceipt.model_validate_json(receipt_path.read_text())
        manifest_path, _manifest = self._run_manifest(run)
        started_at = _utc_now()
        start = time.monotonic()
        self._write_status(run, "RUNNING", started_at=started_at)
        log_path = Path(run.audit_dir) / "executor.log"
        with log_path.open("a", encoding="utf-8") as log:
            completed = subprocess.run(
                self._command(run),
                cwd=self.repo,
                env=self._environment(run),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        wall = time.monotonic() - start
        if completed.returncode != 0:
            self._write_status(
                run,
                "INTERRUPTED",
                started_at=started_at,
                exit_code=completed.returncode,
                wall_seconds=wall,
            )
            raise RuntimeError(
                f"P2-R1 run interrupted: {run.run_id} exit={completed.returncode}"
            )
        analysis_run = collect_p2_r1_arm_run(self.protocol, run)
        ledger_path = Path(run.audit_dir) / "transport_ledger.json"
        ledger = TransportLedgerRecord.model_validate_json(ledger_path.read_text())
        database_path = Path(run.database_path)
        receipt = P2R1RunReceipt(
            protocol_sha256=self.protocol.protocol_sha256,
            executor_commit=self.executor_commit,
            run_id=run.run_id,
            block=run.block,
            arm=run.arm,
            paired_local_seed=run.paired_local_seed,
            state_namespace=run.state_namespace,
            run_manifest_sha256=sha256_file(manifest_path),
            exit_code=completed.returncode,
            started_at=started_at,
            finished_at=_utc_now(),
            wall_seconds=wall,
            transport_ledger_sha256=sha256_file(ledger_path),
            analysis_run=analysis_run,
            scientific_slots=[
                P2R1ScientificSlotReceipt(
                    slot_id=run.slots[index].slot_id,
                    block=run.block,
                    arm=run.arm,
                    slot=index + 1,
                    paired_local_seed=run.paired_local_seed,
                    model=self.protocol.provider.model,
                    rendered_system_prompt_sha256=proposal.rendered_system_prompt_sha256,
                    rendered_user_prompt_sha256=proposal.rendered_user_prompt_sha256,
                    request_payload_sha256=proposal.request_payload_sha256,
                    transport_attempt_count=len(
                        proposal.transport_attempt_payload_sha256s
                    ),
                    transport_attempt_payload_sha256s=(
                        proposal.transport_attempt_payload_sha256s
                    ),
                    terminal_funnel_state=_terminal_class(
                        proposal, analysis_run.baseline_score
                    ),
                    state_namespace=run.state_namespace,
                    executor_commit=self.executor_commit,
                    protocol_sha256=self.protocol.protocol_sha256,
                )
                for index, proposal in enumerate(analysis_run.slots)
            ],
            database_sha256=sha256_file(database_path),
        )
        create_once_json(receipt_path, receipt)
        self._write_status(run, "COMPLETE", receipt_sha256=sha256_file(receipt_path))
        return receipt

    def _execute_block(self, block: int) -> list[P2R1RunReceipt]:
        block_runs = [run for run in self.schedule.runs if run.block == block]
        block_runs.sort(key=lambda run: run.position_in_block)
        return [self._execute_one(run) for run in block_runs]

    def _execute_smoke_one(self, run: P2R1SmokeRunSpec) -> P2R1SmokeArmReceipt:
        audit_dir = Path(run.audit_dir)
        audit_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = audit_dir / "run_manifest.json"
        command = self._command(run, proposal_slots=1)
        run_manifest = {
            "schema_version": "1.0",
            "protocol_sha256": self.protocol.protocol_sha256,
            "executor_commit": self.executor_commit,
            "start_manifest_sha256": sha256_file(self.start_manifest_path),
            "run": run.model_dump(mode="json"),
            "command": command,
            "command_sha256": sha256_object(command),
            "scientific_outcome_authority": "NONE",
        }
        if manifest_path.exists():
            if _load_json(manifest_path) != run_manifest:
                raise RuntimeError("smoke run manifest changed on resume")
        else:
            create_once_json(manifest_path, run_manifest)
        log_path = audit_dir / "executor.log"
        with log_path.open("a", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=self.repo,
                env=self._environment(run),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError(f"P2-R1 smoke failed: {run.run_id} exit={completed.returncode}")
        ledger = TransportLedgerRecord.model_validate_json(
            (audit_dir / "transport_ledger.json").read_text(encoding="utf-8")
        )
        if len(ledger.slots) != 1:
            raise RuntimeError("smoke did not consume exactly one slot")
        programs, attempts = _read_database(Path(run.results_dir))
        proposal = _slot_from_artifacts(1, ledger.slots[0], programs.get(1), attempts.get(1))
        if not proposal.evaluator_reached:
            raise RuntimeError(f"smoke did not reach the evaluator: {run.run_id}")
        return P2R1SmokeArmReceipt(
            arm=run.arm,
            run_id=run.run_id,
            state_namespace=run.state_namespace,
            transport_attempts=len(ledger.slots[0].attempts),
            request_payload_sha256=ledger.slots[0].request_payload_sha256,
            evaluator_reached=True,
            terminal_funnel_state=_terminal_class(
                proposal,
                float(programs[0]["combined_score"]),
            ),
        )

    def _run_remote_smoke(self) -> dict[str, Any]:
        self.admit()
        runs = self._smoke_runs()
        with ThreadPoolExecutor(max_workers=2) as executor:
            arms = list(executor.map(self._execute_smoke_one, runs))
        receipt = P2R1RemoteSmokeReceipt(
            protocol_id=self.protocol.protocol_id,
            protocol_sha256=self.protocol.protocol_sha256,
            executor_commit=self.executor_commit,
            zero_call_e2e_receipt_sha256=_load_json(self.start_manifest_path)[
                "gate_receipt_hashes"
            ]["zero_call_e2e"],
            start_manifest_sha256=sha256_file(self.start_manifest_path),
            arms=arms,
        )
        receipt_path = self.run_root / "remote_smoke_receipt.json"
        if receipt_path.exists():
            existing = P2R1RemoteSmokeReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
            if existing != receipt:
                raise RuntimeError("remote smoke receipt changed on resume")
        else:
            create_once_json(receipt_path, receipt)
        return {
            "status": "REMOTE_SMOKE_PASS",
            "remote_scientific_slots": 2,
            "receipt": str(receipt_path),
            "receipt_sha256": sha256_file(receipt_path),
        }

    def _analyze(self, receipts: list[P2R1RunReceipt]) -> tuple[Path, Path, Any]:
        receipts.sort(key=lambda receipt: (receipt.block, receipt.arm))
        analysis_input = P2R1AnalysisInput(
            protocol_id=self.protocol.protocol_id,
            protocol_sha256=self.protocol.protocol_sha256,
            runs=[receipt.analysis_run for receipt in receipts],
        )
        result = analyze_p2_r1(analysis_input, self.protocol)
        input_path = self.run_root / "p2_r1_analysis_input.json"
        result_path = self.run_root / "p2_r1_analysis_result.json"
        create_once_json(input_path, analysis_input)
        create_once_json(result_path, result)
        return input_path, result_path, result

    def run(self) -> dict[str, Any]:
        if self.execution_mode == "REMOTE_SMOKE":
            return self._run_remote_smoke()
        manifest = self.admit()
        for run in self.schedule.runs:
            self._run_manifest(run)
            if not self._status_path(run).exists():
                self._write_status(run, "PLANNED")
        workers = int(manifest.resources["max_parallel_blocks"])
        receipts: list[P2R1RunReceipt] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._execute_block, block): block
                for block in range(1, 11)
            }
            for future in as_completed(futures):
                receipts.extend(future.result())
        input_path, result_path, result = self._analyze(receipts)
        if self.execution_mode == "ZERO_CALL_E2E":
            receipt = P2R1ZeroCallE2EReceipt(
                protocol_id=self.protocol.protocol_id,
                protocol_sha256=self.protocol.protocol_sha256,
                executor_commit=self.executor_commit,
                production_path=(
                    "DRIVER_ENGINE_STATE_PROMPT_RECEIPT_COLLECTOR_"
                    "FROZEN_ANALYZER_FINAL_ASSESSMENT"
                ),
                start_manifest_sha256=sha256_file(self.start_manifest_path),
                analysis_input_sha256=sha256_file(input_path),
                analysis_result_sha256=sha256_file(result_path),
            )
            receipt_path = self.run_root / "zero_call_e2e_receipt.json"
            create_once_json(receipt_path, receipt)
            return {
                "status": "ZERO_CALL_E2E_PASS",
                "remote_calls": 0,
                "run_count": 20,
                "slot_count": 100,
                "receipt": str(receipt_path),
                "receipt_sha256": sha256_file(receipt_path),
                "final_assessment": str(result_path),
            }
        return {
            "status": "ANALYZED",
            "analysis_input": str(input_path),
            "analysis_result": str(result_path),
            "scientific_outcome": result.scientific_outcome,
            "claim_ceiling": result.claim_ceiling,
        }


def _read_database(results_dir: Path) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    database = results_dir / "programs.sqlite"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        programs = {
            int(row["generation"]): dict(row)
            for row in connection.execute(
                "SELECT generation, code, combined_score, correct, metadata "
                "FROM programs ORDER BY generation, timestamp"
            )
        }
        attempts: dict[int, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT generation, details FROM attempt_log ORDER BY id"
        ):
            details = json.loads(row["details"] or "{}")
            attempts[int(row["generation"])] = details
        return programs, attempts
    finally:
        connection.close()


def _slot_from_artifacts(
    slot_number: int,
    audit_slot: Any | None,
    program: dict[str, Any] | None,
    attempt: dict[str, Any] | None,
) -> ProposalSlot:
    if audit_slot is None:
        return ProposalSlot(
            slot=slot_number,
            model_invocation_started=False,
            proposal_received=False,
            proposal_extracted=False,
            materialized=False,
            compiled=False,
            evaluator_reached=False,
            evaluator_valid=False,
        )
    proposal_received = audit_slot.transport_state == "SUCCEEDED"
    details = attempt or {}
    proposal_extracted = bool(program) or bool(details.get("patch_name"))
    materialized = bool(program) or bool(details.get("generated_code_available"))
    evaluator_reached = bool(program) or bool(details.get("downstream_eval_submitted"))
    compiled = evaluator_reached
    evaluator_valid = bool(program and program.get("correct"))
    score = float(program["combined_score"]) if evaluator_valid else None
    return ProposalSlot(
        slot=slot_number,
        model_invocation_started=True,
        proposal_received=proposal_received,
        proposal_extracted=proposal_received and proposal_extracted,
        materialized=proposal_received and proposal_extracted and materialized,
        compiled=(
            proposal_received and proposal_extracted and materialized and compiled
        ),
        evaluator_reached=(
            proposal_received
            and proposal_extracted
            and materialized
            and compiled
            and evaluator_reached
        ),
        evaluator_valid=evaluator_valid,
        score=score,
        rendered_system_prompt_sha256=audit_slot.rendered_system_prompt_sha256,
        rendered_user_prompt_sha256=audit_slot.rendered_user_prompt_sha256,
        request_payload_sha256=audit_slot.request_payload_sha256,
        transport_attempt_payload_sha256s=[
            item.payload_sha256 for item in audit_slot.attempts
        ],
    )


def collect_p2_r1_arm_run(
    protocol: P2R1Protocol, run: P2R1RunSpec
) -> ArmRun:
    results_dir = Path(run.results_dir)
    ledger_path = Path(run.audit_dir) / "transport_ledger.json"
    ledger = TransportLedgerRecord.model_validate_json(ledger_path.read_text())
    programs, attempts = _read_database(results_dir)
    baseline = programs.get(0)
    if baseline is None:
        raise RuntimeError(f"run has no baseline: {run.run_id}")
    initial_sha = sha256_bytes(str(baseline["code"]).encode("utf-8"))
    if initial_sha != protocol.frozen_assets["initial_program"].sha256:
        raise RuntimeError(f"run baseline changed: {run.run_id}")
    slots = [
        _slot_from_artifacts(
            number,
            ledger.slots[number - 1] if number <= len(ledger.slots) else None,
            programs.get(number),
            attempts.get(number),
        )
        for number in range(1, 6)
    ]
    terminal_generations = set(programs) | set(attempts)
    for audit_slot, proposal_slot in zip(ledger.slots, slots):
        if audit_slot.slot in terminal_generations:
            audit_slot.scientific_state = "COMPLETE"
        audit_slot.terminal_funnel_state = _terminal_class(
            proposal_slot, float(baseline["combined_score"])
        )
    atomic_write_json(ledger_path, ledger)
    responses = [
        json.loads(path.read_text())
        for path in sorted((Path(run.audit_dir) / "responses").glob("slot_*.json"))
    ]
    observed_input = sum(int(response.get("input_tokens", 0)) for response in responses)
    observed_output = sum(int(response.get("output_tokens", 0)) for response in responses)
    observed_cost = sum(float(response.get("cost", 0.0)) for response in responses)
    metadata = [
        json.loads(program.get("metadata") or "{}") for program in programs.values()
    ]
    starts = [float(item["pipeline_started_at"]) for item in metadata if item.get("pipeline_started_at")]
    finishes = [float(item["postprocess_finished_at"]) for item in metadata if item.get("postprocess_finished_at")]
    wall_seconds = max(finishes) - min(starts) if starts and finishes else 0.0
    return ArmRun(
        block=run.block,
        arm=run.arm,
        baseline_score=float(baseline["combined_score"]),
        initial_program_sha256=protocol.frozen_assets["initial_program"].sha256,
        evaluator_sha256=protocol.frozen_assets["evaluator"].sha256,
        config_sha256=protocol.frozen_assets["config"].sha256,
        initial_incumbent_sha256=initial_sha,
        state_namespace=run.state_namespace,
        slots=slots,
        observed_input_tokens=observed_input,
        observed_output_tokens=observed_output,
        observed_cost=observed_cost,
        wall_seconds=max(0.0, wall_seconds),
        resume_consistent=all(
            slot.scientific_state == "COMPLETE" for slot in ledger.slots
        ),
    )


def run_p2_r1_execution(
    *,
    repo: Path,
    protocol_path: Path,
    run_root: Path,
    execution_mode: EXECUTION_MODE,
    max_parallel_blocks: int | None,
    zero_call_receipt: Path | None = None,
    remote_smoke_receipt: Path | None = None,
) -> dict[str, Any]:
    return P2R1ExecutionDriver(
        repo=repo,
        protocol_path=protocol_path,
        run_root=run_root,
        execution_mode=execution_mode,
        max_parallel_blocks=max_parallel_blocks,
        zero_call_receipt=zero_call_receipt,
        remote_smoke_receipt=remote_smoke_receipt,
    ).run()


__all__ = [
    "P2R1ExecutionDriver",
    "P2R1RunManifest",
    "P2R1RunReceipt",
    "P2R1ScientificSlotReceipt",
    "P2R1RunSpec",
    "P2R1Schedule",
    "P2R1SlotSpec",
    "P2R1StartManifest",
    "build_p2_r1_schedule",
    "collect_p2_r1_arm_run",
    "run_p2_r1_execution",
]
