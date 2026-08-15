from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from evidence_evolve.artifacts import create_once_bytes, create_once_json, environment_receipt
from evidence_evolve.hashing import sha256_bytes, sha256_file, sha256_object
from evidence_evolve.proposals.materializer import (
    ProposalMaterializationError,
    extract_search_replace_proposal,
    materialize_proposal,
)
from evidence_evolve.proposals.models import (
    CandidateSurvivalFunnel,
    MechanicsAdmissionProtocol,
    MechanicsAdmissionReceipt,
    MechanicsCaseReceipt,
)


def available_cpu_count() -> int:
    """Return usable CPUs, respecting affinity and cgroup CPU quota."""
    try:
        affinity_count = max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        affinity_count = max(1, os.cpu_count() or 1)
    quota_count = affinity_count
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    try:
        quota, period = cpu_max.read_text().split()
        if quota != "max":
            quota_count = max(1, math.ceil(int(quota) / int(period)))
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return min(affinity_count, quota_count)


def load_mechanics_protocol(path: Path) -> MechanicsAdmissionProtocol:
    return MechanicsAdmissionProtocol.model_validate(json.loads(path.read_text()))


def _artifact(repo: Path, relative: str, expected_sha256: str) -> Path:
    path = (repo / relative).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError as error:
        raise ValueError(f"artifact escapes repository: {relative}") from error
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValueError(
            f"artifact hash mismatch for {relative}: "
            f"expected={expected_sha256} actual={actual}"
        )
    return path


def _load_corpus(path: Path) -> dict[str, dict[str, Any]]:
    cases = {}
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            case = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid corpus JSON at line {line_number}") from error
        case_id = case.get("case_id")
        if not isinstance(case_id, str):
            raise ValueError(f"corpus line {line_number} has no case_id")
        if case_id in cases:
            raise ValueError(f"duplicate corpus case_id: {case_id}")
        cases[case_id] = case
    return cases


def _safe_case_id(case_id: str) -> str:
    safe_characters = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    )
    if not case_id or any(character not in safe_characters for character in case_id):
        raise ValueError(f"unsafe case_id: {case_id}")
    return case_id


def _case_receipt(
    *,
    case: dict[str, Any],
    target_bytes: bytes,
    evaluator: Path,
    baseline_score: float,
    timeout_seconds: int,
    python_executable: str,
    case_dir: Path,
    receipt_root: Path,
) -> MechanicsCaseReceipt:
    case_id = _safe_case_id(str(case["case_id"]))
    arm = str(case.get("arm", "unknown"))
    target_sha256 = sha256_bytes(target_bytes)
    common = {
        "case_id": case_id,
        "arm": arm,
        "target_sha256": target_sha256,
    }
    raw_response = case.get("raw_llm_response")
    patch_text = case.get("extracted_patch_text")
    if not isinstance(raw_response, str) or not isinstance(patch_text, str):
        return MechanicsCaseReceipt(
            **common,
            proposal_extracted=False,
            patch_applied=False,
            candidate_compiled=False,
            evaluator_invoked=False,
            evaluator_reached=False,
            failure_stage="proposal_extraction",
            failure_reason="corpus case lacks raw response or extracted patch text",
        )
    if sha256_bytes(raw_response.encode()) != case.get("raw_llm_response_sha256"):
        raise ValueError(f"raw response hash mismatch: {case_id}")
    if sha256_bytes(patch_text.encode()) != case.get("extracted_patch_sha256"):
        raise ValueError(f"extracted patch hash mismatch: {case_id}")
    if case.get("target_program_sha256") != target_sha256:
        raise ValueError(f"case target hash mismatch: {case_id}")

    case_dir.mkdir(parents=True, exist_ok=False)
    create_once_bytes(case_dir / "raw_response.txt", raw_response.encode())
    create_once_bytes(case_dir / "extracted_patch.txt", patch_text.encode())
    create_once_bytes(case_dir / "target.py", target_bytes)
    try:
        proposal = extract_search_replace_proposal(
            proposal_id=case_id,
            source_system=arm,
            raw_response=raw_response,
            target_sha256=target_sha256,
            extracted_patch_text=patch_text,
            metadata={
                "source_run_id": case.get("run_id"),
                "source_generation": case.get("generation"),
                "source_failure_sha256": case.get("failure_sha256"),
            },
        )
        create_once_json(case_dir / "proposal_ir.json", proposal)
    except (ProposalMaterializationError, ValueError) as error:
        receipt = MechanicsCaseReceipt(
            **common,
            proposal_extracted=False,
            patch_applied=False,
            candidate_compiled=False,
            evaluator_invoked=False,
            evaluator_reached=False,
            failure_stage="proposal_extraction",
            failure_reason=str(error),
        )
        create_once_json(case_dir / "case_receipt.json", receipt)
        return receipt
    try:
        candidate, materialization = materialize_proposal(proposal, target_bytes)
        create_once_json(
            case_dir / "materialization_receipt.json", materialization
        )
        candidate_path = case_dir / "candidate.py"
        create_once_bytes(candidate_path, candidate.encode())
    except ProposalMaterializationError as error:
        receipt = MechanicsCaseReceipt(
            **common,
            proposal_extracted=True,
            patch_applied=False,
            candidate_compiled=False,
            evaluator_invoked=False,
            evaluator_reached=False,
            failure_stage="materialization",
            failure_reason=f"{error.code}: {error}",
        )
        create_once_json(case_dir / "case_receipt.json", receipt)
        return receipt
    match_modes = [edit.match_mode for edit in materialization.applied_edits]
    relative_candidate = str(candidate_path.relative_to(receipt_root))
    try:
        compile(candidate, relative_candidate, "exec")
    except (SyntaxError, ValueError) as error:
        receipt = MechanicsCaseReceipt(
            **common,
            proposal_extracted=True,
            patch_applied=True,
            candidate_compiled=False,
            evaluator_invoked=False,
            evaluator_reached=False,
            candidate_sha256=materialization.candidate_sha256,
            match_modes=match_modes,
            failure_stage="compile",
            failure_reason=str(error),
            candidate_path=relative_candidate,
        )
        create_once_json(case_dir / "case_receipt.json", receipt)
        return receipt

    results_dir = case_dir / "evaluator_results"
    evaluator_env = os.environ.copy()
    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        evaluator_env[variable] = "1"
    try:
        process = subprocess.run(
            [
                python_executable,
                str(evaluator),
                "--program_path",
                str(candidate_path),
                "--results_dir",
                str(results_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=evaluator_env,
        )
        create_once_bytes(case_dir / "evaluator.stdout", process.stdout.encode())
        create_once_bytes(case_dir / "evaluator.stderr", process.stderr.encode())
    except subprocess.TimeoutExpired as error:
        receipt = MechanicsCaseReceipt(
            **common,
            proposal_extracted=True,
            patch_applied=True,
            candidate_compiled=True,
            evaluator_invoked=True,
            evaluator_reached=False,
            candidate_sha256=materialization.candidate_sha256,
            match_modes=match_modes,
            failure_stage="evaluator_timeout",
            failure_reason=str(error),
            candidate_path=relative_candidate,
            evaluator_results_dir=str(results_dir.relative_to(receipt_root)),
        )
        create_once_json(case_dir / "case_receipt.json", receipt)
        return receipt
    metrics_path = results_dir / "metrics.json"
    correct_path = results_dir / "correct.json"
    evaluator_reached = (
        process.returncode == 0 and metrics_path.is_file() and correct_path.is_file()
    )
    candidate_score = None
    evaluator_valid = None
    if evaluator_reached:
        metrics = json.loads(metrics_path.read_text())
        correct = json.loads(correct_path.read_text())
        value = metrics.get("combined_score")
        if isinstance(value, (int, float)):
            candidate_score = float(value)
        valid_value = correct.get("correct")
        if isinstance(valid_value, bool):
            evaluator_valid = valid_value
    receipt = MechanicsCaseReceipt(
        **common,
        proposal_extracted=True,
        patch_applied=True,
        candidate_compiled=True,
        evaluator_invoked=True,
        evaluator_reached=evaluator_reached,
        evaluator_valid=evaluator_valid,
        candidate_score=candidate_score,
        nonbaseline_score=(
            candidate_score is not None and candidate_score != baseline_score
        ),
        candidate_sha256=materialization.candidate_sha256,
        match_modes=match_modes,
        failure_stage=None if evaluator_reached else "evaluator",
        failure_reason=(
            None
            if evaluator_reached
            else f"exit={process.returncode} metrics={metrics_path.is_file()} "
            f"correct={correct_path.is_file()}"
        ),
        candidate_path=relative_candidate,
        evaluator_results_dir=str(results_dir.relative_to(receipt_root)),
    )
    create_once_json(case_dir / "case_receipt.json", receipt)
    return receipt


def _funnel(cases: list[MechanicsCaseReceipt]) -> CandidateSurvivalFunnel:
    proposals = len(cases)
    extracted = sum(case.proposal_extracted for case in cases)
    patchable = sum(case.patch_applied for case in cases)
    runnable = sum(case.candidate_compiled for case in cases)
    reached = sum(case.evaluator_reached for case in cases)
    valid = sum(case.evaluator_valid is True for case in cases)
    nonbaseline = sum(case.nonbaseline_score for case in cases)
    return CandidateSurvivalFunnel(
        proposals=proposals,
        proposal_extracted=extracted,
        patchable=patchable,
        runnable=runnable,
        evaluator_reached=reached,
        evaluator_valid=valid,
        nonbaseline_score=nonbaseline,
        patch_apply_success_rate=patchable / proposals,
        candidate_compile_success_rate=runnable / proposals,
        evaluator_reached_rate=reached / proposals,
    )


def run_mechanics_admission(
    *,
    protocol_path: Path,
    repo: Path,
    run_dir: Path,
    max_workers: int | None = None,
    python_executable: str = sys.executable,
) -> MechanicsAdmissionReceipt:
    started_at = time.monotonic()
    repo = repo.resolve()
    protocol_path = protocol_path.resolve()
    protocol = load_mechanics_protocol(protocol_path)
    corpus_path = _artifact(repo, protocol.corpus_path, protocol.corpus_sha256)
    target_path = _artifact(
        repo, protocol.target_program_path, protocol.target_program_sha256
    )
    evaluator_path = _artifact(repo, protocol.evaluator_path, protocol.evaluator_sha256)
    corpus = _load_corpus(corpus_path)
    missing = [case_id for case_id in protocol.case_ids if case_id not in corpus]
    if missing:
        raise ValueError(f"protocol cases missing from corpus: {missing}")
    if max_workers is not None and max_workers < 1:
        raise ValueError("max_workers must be positive")
    target_bytes = target_path.read_bytes()
    run_dir = run_dir.resolve()
    cases_dir = run_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=False)
    available_cpus = available_cpu_count()
    worker_count = min(
        len(protocol.case_ids), max_workers or min(len(protocol.case_ids), available_cpus)
    )
    def execute(case_id: str) -> MechanicsCaseReceipt:
        return _case_receipt(
            case=corpus[case_id],
            target_bytes=target_bytes,
            evaluator=evaluator_path,
            baseline_score=protocol.baseline_score,
            timeout_seconds=protocol.evaluator_timeout_seconds,
            python_executable=python_executable,
            case_dir=cases_dir / _safe_case_id(case_id),
            receipt_root=run_dir,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        cases = list(executor.map(execute, protocol.case_ids))
    funnel = _funnel(cases)
    arms = sorted({case.arm for case in cases})
    per_arm = {
        arm: _funnel([case for case in cases if case.arm == arm]) for arm in arms
    }
    thresholds = protocol.thresholds
    checks = {
        "patch_apply_success_rate": (
            funnel.patch_apply_success_rate
            >= thresholds.patch_apply_success_rate_min
        ),
        "candidate_compile_success_rate": (
            funnel.candidate_compile_success_rate
            >= thresholds.candidate_compile_success_rate_min
        ),
        "evaluator_reached_rate": (
            funnel.evaluator_reached_rate >= thresholds.evaluator_reached_rate_min
        ),
        "nonbaseline_candidate_score_per_arm": all(
            arm_funnel.nonbaseline_score
            >= thresholds.nonbaseline_candidate_score_required_per_arm
            for arm_funnel in per_arm.values()
        ),
    }
    admitted = all(checks.values())
    receipt = MechanicsAdmissionReceipt(
        protocol_id=protocol.protocol_id,
        protocol_sha256=sha256_object(protocol),
        source_campaign=protocol.source_campaign,
        mechanics_status="PASS" if admitted else "FAIL",
        admitted_for_expensive_search=admitted,
        failure_outcome=None if admitted else "INVALID_MECHANICS_OR_ADAPTER",
        elapsed_seconds=time.monotonic() - started_at,
        implementation_hashes={
            "admission.py": sha256_file(Path(__file__)),
            "materializer.py": sha256_file(Path(__file__).with_name("materializer.py")),
            "models.py": sha256_file(Path(__file__).with_name("models.py")),
            "shinka_adapter.py": sha256_file(
                Path(__file__).with_name("shinka_adapter.py")
            ),
        },
        environment=environment_receipt(
            {
                "max_workers": str(worker_count),
                "available_cpu_count": str(available_cpus),
                "host_logical_cpu_count": str(os.cpu_count()),
                "remote_model_calls": "0",
            }
        ),
        thresholds=thresholds,
        funnel=funnel,
        per_arm_funnel=per_arm,
        threshold_checks=checks,
        cases=cases,
        observation=(
            "Mechanics admission only; this receipt does not establish search quality, "
            "non-inferiority, scientific headroom, or an efficiency win."
        ),
    )
    create_once_json(run_dir / "mechanics_admission_receipt.json", receipt)
    return receipt
