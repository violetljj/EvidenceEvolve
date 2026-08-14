from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.artifacts import (
    atomic_write_json,
    environment_receipt,
    load_receipt,
    write_receipt,
)
from evidence_evolve.budgets import BudgetLedger
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.gate_engine import GateEngine
from evidence_evolve.governance.protocol_lock import ProtocolLock, dump_contract, load_contract
from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.models import (
    CandidateGenome,
    EvaluationInput,
    EvaluationReceipt,
    MechanicsStatus,
    ResearchStage,
    ScientificOutcome,
)
from evidence_evolve.worktrees import WorktreeManager


def _git(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=worktree, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def build_onnx_evaluation(
    *,
    contract_sha256: str,
    candidate: CandidateGenome,
    stage: ResearchStage,
    changed_files: list[str],
    protocol_violations: list[str],
    raw: dict[str, object],
) -> EvaluationInput:
    mechanics = MechanicsStatus(str(raw["mechanics_status"]))
    controls = dict(raw["controls"])  # type: ignore[arg-type]
    improved = bool(raw.get("improved", False))
    outcome = (
        ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
        if mechanics is MechanicsStatus.FAIL
        else ScientificOutcome.POSITIVE_HEADROOM
        if improved and all(controls.values())
        else ScientificOutcome.VALID_NEGATIVE
    )
    return EvaluationInput(
        contract_sha256=contract_sha256,
        candidate=candidate,
        stage=stage,
        changed_files=changed_files,
        protocol_violations=protocol_violations,
        mechanics_status=mechanics,
        data_eligible=True,
        metrics=dict(raw["metrics"]),  # type: ignore[arg-type]
        controls=controls,
        scientific_outcome=outcome,
    )


def evaluate_onnx_candidate(
    contract_path: Path,
    proposal_path: Path,
    repo_root: Path,
    worktree: Path,
    run_dir: Path,
    *,
    confirmation: bool = False,
) -> dict[str, object]:
    contract = load_contract(contract_path)
    validation = ProtocolLock(repo_root).assert_valid(contract)
    candidate = CandidateGenome.model_validate(
        json.loads(proposal_path.read_text(encoding="utf-8"))
    )
    stage = (
        ResearchStage.C0_CONFIRMATION
        if confirmation
        else ResearchStage.M0_MECHANICS
    )
    candidate_dir = run_dir / "candidates" / candidate.candidate_id
    receipt_path = candidate_dir / "receipts" / f"{stage.value}.json"
    manager = WorktreeManager(repo_root)
    changed_files = manager.changed_files(worktree, contract.campaign.base_commit)
    registry = ClosureRegistry.load(repo_root / contract.closure_registry)
    audit = audit_candidate(
        contract, candidate, registry, changed_files=changed_files
    )
    database = run_dir / "research.db"
    budgets = BudgetLedger(database, contract.budgets)
    for category in ("proposal_calls", "implementations", "mechanics_runs", "proxy_runs"):
        budgets.reserve(category, 1, f"{category}:{candidate.candidate_id}")
    if confirmation:
        budgets.reserve(
            "confirmation_runs", 1, f"confirmation_runs:{candidate.candidate_id}"
        )

    if receipt_path.exists():
        envelope = load_receipt(receipt_path)
        if envelope.receipt.candidate_id != candidate.candidate_id:
            raise ValueError("existing receipt candidate mismatch")
        ArchiveStore(database).record(
            candidate, envelope, receipt_path.relative_to(run_dir)
        )
        return {
            "candidate_id": candidate.candidate_id,
            "stage": stage.value,
            "verdict": envelope.receipt.verdict.model_dump(mode="json"),
            "metrics": envelope.receipt.evaluation_input.metrics,
            "budgets": budgets.snapshot(),
            "receipt": str(receipt_path),
            "resumed": True,
        }

    candidate_path = worktree / "tasks/onnx_rewrite/candidates/candidate.py"
    from tasks.onnx_rewrite.evaluator import evaluate as raw_evaluate

    started = perf_counter()
    raw = raw_evaluate(candidate_path, confirmation=confirmation)
    elapsed = perf_counter() - started
    evaluation = build_onnx_evaluation(
        contract_sha256=validation.contract_sha256 or "",
        candidate=candidate,
        stage=stage,
        changed_files=changed_files,
        protocol_violations=audit.violations,
        raw=raw,
    )
    verdict = GateEngine(contract).evaluate(evaluation)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = candidate_dir / "stages" / stage.value
    patch = subprocess.run(
        ["git", "diff", "--binary", contract.campaign.base_commit, "HEAD", "--"],
        cwd=worktree,
        check=True,
        capture_output=True,
    ).stdout
    stage_dir.mkdir(parents=True, exist_ok=True)
    (stage_dir / "patch.diff").write_bytes(patch)
    atomic_write_json(stage_dir / "proposal.json", candidate)
    atomic_write_json(stage_dir / "metrics.json", raw)
    atomic_write_json(stage_dir / "controls.json", evaluation.controls)
    atomic_write_json(stage_dir / "gates.json", verdict)
    atomic_write_json(
        stage_dir / "code_manifest.json",
        {
            "changed_files": changed_files,
            "patch_sha256": sha256_bytes(patch),
            "candidate_commit": _git(worktree, "rev-parse", "HEAD"),
        },
    )
    atomic_write_json(
        stage_dir / "evidence_manifest.json",
        {
            "contract_sha256": validation.contract_sha256,
            "stage": evaluation.stage.value,
            "data_eligible": True,
            "confirmation": confirmation,
        },
    )
    logs = stage_dir / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "stdout.log").write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    (logs / "stderr.log").write_text(str(raw.get("error", "")), encoding="utf-8")
    receipt = EvaluationReceipt(
        receipt_id=f"{contract.campaign.id}:{candidate.candidate_id}:{evaluation.stage.value}",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        campaign_id=contract.campaign.id,
        candidate_id=candidate.candidate_id,
        base_commit=contract.campaign.base_commit,
        candidate_commit=_git(worktree, "rev-parse", "HEAD"),
        patch_sha256=sha256_bytes(patch),
        evaluator_hashes={
            asset.asset_id: asset.sha256 or ""
            for asset in contract.frozen_assets
            if asset.kind.value == "evaluator"
        },
        data_hashes={source.source_id: source.sha256 or "" for source in contract.evidence_sources},
        seed=int(raw.get("seed", 0)),
        command=["evolve", "evaluate-onnx-candidate", candidate.candidate_id],
        elapsed_seconds=elapsed,
        environment=environment_receipt(
            {"task": "onnx_rewrite_r0", "onnx_provider": "CPUExecutionProvider"}
        ),
        evaluation_input=evaluation,
        verdict=verdict,
    )
    envelope = write_receipt(receipt_path, receipt)
    ArchiveStore(database).record(candidate, envelope, receipt_path.relative_to(run_dir))
    if not (run_dir / "contract.locked.yaml").exists():
        dump_contract(contract, run_dir / "contract.locked.yaml")
    atomic_write_json(
        run_dir / "run_manifest.json",
        {
            "campaign_id": contract.campaign.id,
            "contract_sha256": validation.contract_sha256,
            "base_commit": contract.campaign.base_commit,
            "claim_scope": contract.campaign.claim_scope,
        },
    )
    return {
        "candidate_id": candidate.candidate_id,
        "stage": evaluation.stage.value,
        "verdict": verdict.model_dump(mode="json"),
        "metrics": raw["metrics"],
        "budgets": budgets.snapshot(),
        "receipt": str(receipt_path),
    }
