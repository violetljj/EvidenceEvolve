from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from evidence_evolve.artifacts import load_receipt
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.gate_engine import GateEngine
from evidence_evolve.governance.protocol_lock import ProtocolLock, load_contract
from evidence_evolve.hashing import sha256_bytes
from evidence_evolve.models import EvaluationInput, ReceiptEnvelope, ResearchStage


def receipt_paths(run_dir: Path) -> list[Path]:
    current = (
        path
        for path in (run_dir / "candidates").glob("*/receipts/*.json")
        if not path.name.endswith(".mechanism.json")
    )
    legacy = (run_dir / "candidates").glob("*/reproducibility_receipt.json")
    return sorted({*current, *legacy})


def _expected_hashes(contract: object, kind: str) -> dict[str, str]:
    return {
        asset.asset_id: asset.sha256 or ""
        for asset in contract.frozen_assets
        if asset.kind.value == kind
    }


def _binding_failures(
    envelope: ReceiptEnvelope,
    *,
    contract: object,
    contract_sha256: str,
) -> list[str]:
    receipt = envelope.receipt
    failures: list[str] = []
    if receipt.campaign_id != contract.campaign.id:
        failures.append("CAMPAIGN_ID_MISMATCH")
    if receipt.base_commit != contract.campaign.base_commit:
        failures.append("BASE_COMMIT_MISMATCH")
    if receipt.evaluation_input.contract_sha256 != contract_sha256:
        failures.append("CONTRACT_SHA256_MISMATCH")
    if receipt.evaluator_hashes != _expected_hashes(contract, "evaluator"):
        failures.append("EVALUATOR_HASHES_MISMATCH")
    expected_data = {
        source.source_id: source.sha256 or "" for source in contract.evidence_sources
    }
    if receipt.data_hashes != expected_data:
        failures.append("DATA_HASHES_MISMATCH")
    return failures


def replay_verdict(run_dir: Path, repo_root: Path) -> dict[str, object]:
    contract = load_contract(run_dir / "contract.locked.yaml")
    report = ProtocolLock(repo_root).assert_valid(contract)
    gate = GateEngine(contract)
    failures: list[dict[str, object]] = []
    paths = receipt_paths(run_dir)
    for receipt_path in paths:
        envelope = load_receipt(receipt_path)
        reasons = _binding_failures(
            envelope,
            contract=contract,
            contract_sha256=report.contract_sha256 or "",
        )
        replayed_verdict = gate.evaluate(envelope.receipt.evaluation_input)
        if replayed_verdict != envelope.receipt.verdict:
            reasons.append("VERDICT_MISMATCH")
        if reasons:
            failures.append(
                {
                    "receipt_id": envelope.receipt.receipt_id,
                    "reasons": sorted(set(reasons)),
                }
            )
    return {
        "mode": "VERDICT_REPLAY",
        "passed": bool(paths) and not failures,
        "replayed": len(paths),
        "failures": failures,
    }


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout


def _git_lines(repo_root: Path, *args: str) -> list[str]:
    return [
        line.strip()
        for line in _git_bytes(repo_root, *args).decode("utf-8").splitlines()
        if line.strip()
    ]


def _without_observational_metrics(evaluation: EvaluationInput) -> dict[str, object]:
    payload = evaluation.model_dump(mode="json")
    payload["metrics"] = {
        name: value
        for name, value in payload["metrics"].items()
        if not name.endswith("latency_ms")
    }
    return payload


def _replay_synthetic(
    envelope: ReceiptEnvelope,
    *,
    contract: object,
    contract_sha256: str,
    repo_root: Path,
) -> EvaluationInput:
    from evidence_evolve.canary import build_canary_evaluation
    from tasks.synthetic_canary.evaluator import evaluate as raw_evaluate

    receipt = envelope.receipt
    if len(receipt.command) != 2 or receipt.command[0] != "synthetic-canary":
        raise ValueError("SYNTHETIC_REPLAY_COMMAND_INVALID")
    raw = raw_evaluate(receipt.command[1])
    registry = ClosureRegistry.load(repo_root / contract.closure_registry)
    audit = audit_candidate(
        contract,
        receipt.evaluation_input.candidate,
        registry,
        changed_files=receipt.evaluation_input.changed_files,
    )
    return build_canary_evaluation(
        contract_sha256=contract_sha256,
        candidate=receipt.evaluation_input.candidate,
        changed_files=receipt.evaluation_input.changed_files,
        protocol_violations=audit.violations,
        raw=raw,
    )


def _replay_onnx(
    envelope: ReceiptEnvelope,
    *,
    contract: object,
    contract_sha256: str,
    repo_root: Path,
) -> EvaluationInput:
    from evidence_evolve.onnx_campaign import build_onnx_evaluation
    from tasks.onnx_rewrite.evaluator import evaluate as raw_evaluate

    receipt = envelope.receipt
    if not receipt.candidate_commit or not receipt.patch_sha256:
        raise ValueError("CANDIDATE_COMMIT_OR_PATCH_MISSING")
    patch = _git_bytes(
        repo_root,
        "diff",
        "--binary",
        receipt.base_commit,
        receipt.candidate_commit,
        "--",
    )
    if sha256_bytes(patch) != receipt.patch_sha256:
        raise ValueError("PATCH_SHA256_MISMATCH")
    changed_files = _git_lines(
        repo_root,
        "diff",
        "--name-only",
        receipt.base_commit,
        receipt.candidate_commit,
        "--",
    )
    candidate_source = _git_bytes(
        repo_root,
        "show",
        f"{receipt.candidate_commit}:tasks/onnx_rewrite/candidates/candidate.py",
    )
    with tempfile.TemporaryDirectory(prefix="evidence-evolve-replay-") as directory:
        candidate_path = Path(directory) / "candidate.py"
        candidate_path.write_bytes(candidate_source)
        confirmation = receipt.evaluation_input.stage is ResearchStage.C0_CONFIRMATION
        raw = raw_evaluate(candidate_path, confirmation=confirmation)
    registry = ClosureRegistry.load(repo_root / contract.closure_registry)
    audit = audit_candidate(
        contract,
        receipt.evaluation_input.candidate,
        registry,
        changed_files=changed_files,
    )
    return build_onnx_evaluation(
        contract_sha256=contract_sha256,
        candidate=receipt.evaluation_input.candidate,
        stage=receipt.evaluation_input.stage,
        changed_files=changed_files,
        protocol_violations=audit.violations,
        raw=raw,
    )


EVALUATION_REPLAY_ADAPTERS = {
    "synthetic_canary_r0": _replay_synthetic,
    "onnx_rewrite_r0": _replay_onnx,
    "onnx_rewrite_r1": _replay_onnx,
    "onnx_rewrite_r1_2_a0": _replay_onnx,
}


def replay_evaluation(run_dir: Path, repo_root: Path) -> dict[str, object]:
    contract = load_contract(run_dir / "contract.locked.yaml")
    report = ProtocolLock(repo_root).assert_valid(contract)
    contract_sha256 = report.contract_sha256 or ""
    gate = GateEngine(contract)
    failures: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    paths = receipt_paths(run_dir)
    replay_adapter = EVALUATION_REPLAY_ADAPTERS.get(contract.campaign.id)
    if replay_adapter is None:
        return {
            "mode": "EVALUATION_REPLAY",
            "passed": False,
            "replayed": 0,
            "failures": [
                {
                    "receipt_id": None,
                    "reasons": [f"NO_REPLAY_ADAPTER:{contract.campaign.id}"],
                }
            ],
            "warnings": [],
        }

    for receipt_path in paths:
        envelope = load_receipt(receipt_path)
        reasons = _binding_failures(
            envelope,
            contract=contract,
            contract_sha256=contract_sha256,
        )
        try:
            replayed_input = replay_adapter(
                envelope,
                contract=contract,
                contract_sha256=contract_sha256,
                repo_root=repo_root,
            )
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            reasons.append(f"REEXECUTION_FAILED:{type(exc).__name__}:{exc}")
        else:
            if _without_observational_metrics(replayed_input) != _without_observational_metrics(
                envelope.receipt.evaluation_input
            ):
                reasons.append("EVALUATION_INPUT_MISMATCH")
            replayed_verdict = gate.evaluate(replayed_input)
            if replayed_verdict != envelope.receipt.verdict:
                reasons.append("VERDICT_MISMATCH")
            recorded_latency = {
                name: value
                for name, value in envelope.receipt.evaluation_input.metrics.items()
                if name.endswith("latency_ms")
            }
            replayed_latency = {
                name: value
                for name, value in replayed_input.metrics.items()
                if name.endswith("latency_ms")
            }
            if recorded_latency != replayed_latency:
                warnings.append(
                    {
                        "receipt_id": envelope.receipt.receipt_id,
                        "reason": "OBSERVATIONAL_LATENCY_CHANGED",
                        "recorded": recorded_latency,
                        "replayed": replayed_latency,
                    }
                )
        if reasons:
            failures.append(
                {
                    "receipt_id": envelope.receipt.receipt_id,
                    "reasons": sorted(set(reasons)),
                }
            )
    return {
        "mode": "EVALUATION_REPLAY",
        "passed": bool(paths) and not failures,
        "replayed": len(paths),
        "failures": failures,
        "warnings": warnings,
    }


def replay_run(run_dir: Path, repo_root: Path) -> dict[str, object]:
    """Backward-compatible name for explicitly verdict-only replay."""
    return replay_verdict(run_dir, repo_root)
