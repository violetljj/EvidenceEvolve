from __future__ import annotations

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
from evidence_evolve.governance.protocol_lock import (
    ProtocolLock,
    dump_contract,
    load_contract,
)
from evidence_evolve.hashing import sha256_object
from evidence_evolve.models import (
    CandidateGenome,
    EvaluationInput,
    EvaluationReceipt,
    ExpectedSignature,
    MechanicsStatus,
    MutationType,
    ResearchStage,
    ScientificOutcome,
)
from tasks.synthetic_canary.evaluator import evaluate as raw_evaluate


CANARY_EXPECTATIONS = {
    "CANARY-PROTOCOL-TAMPER": "INVALID_PROTOCOL_TAMPERING",
    "CANARY-CLOSED-FAMILY": "INVALID_PROTOCOL_TAMPERING",
    "CANARY-NOT-EVALUABLE": "PAUSE_NOT_EVALUABLE",
    "CANARY-SAFETY-REGRESSION": "KILL",
    "CANARY-VALID-POSITIVE": "ADMIT",
}


def _candidate(candidate_id: str, family: str = "canary_new_family") -> CandidateGenome:
    return CandidateGenome(
        candidate_id=candidate_id,
        parent_ids=["CANARY-SEED"],
        island="mechanics",
        family=family,
        mutation_type=MutationType.MECHANISM,
        hypothesis="A deterministic canary intervention should expose the intended gate semantics.",
        intervention="Apply one bounded synthetic intervention to the canary output.",
        expected_signature=ExpectedSignature(
            improve=["clearance_mae_delta"],
            unchanged=["false_block_delta_pp"],
        ),
        falsifier="A trap receives a gate decision different from its frozen expectation.",
        required_controls=["wrong_factor", "zero_factor"],
        editable_files=["tasks/synthetic_canary/candidates/candidate.py"],
        estimated_cost_tier=0,
    )


def _scenario_definitions() -> list[tuple[str, str, CandidateGenome, list[str]]]:
    return [
        (
            "protocol_tamper",
            "CANARY-PROTOCOL-TAMPER",
            _candidate("CANARY-PROTOCOL-TAMPER"),
            ["tasks/synthetic_canary/evaluator.py"],
        ),
        (
            "closed_family",
            "CANARY-CLOSED-FAMILY",
            _candidate("CANARY-CLOSED-FAMILY", family="query_local_ray_plane"),
            ["tasks/synthetic_canary/candidates/candidate.py"],
        ),
        (
            "not_evaluable",
            "CANARY-NOT-EVALUABLE",
            _candidate("CANARY-NOT-EVALUABLE"),
            ["tasks/synthetic_canary/candidates/candidate.py"],
        ),
        (
            "safety_regression",
            "CANARY-SAFETY-REGRESSION",
            _candidate("CANARY-SAFETY-REGRESSION"),
            ["tasks/synthetic_canary/candidates/candidate.py"],
        ),
        (
            "valid_positive",
            "CANARY-VALID-POSITIVE",
            _candidate("CANARY-VALID-POSITIVE"),
            ["tasks/synthetic_canary/candidates/candidate.py"],
        ),
    ]


def _write_candidate_artifacts(
    candidate_dir: Path,
    candidate: CandidateGenome,
    evaluation: EvaluationInput,
    verdict: object,
) -> None:
    atomic_write_json(candidate_dir / "proposal.json", candidate)
    atomic_write_json(candidate_dir / "code_manifest.json", {
        "changed_files": evaluation.changed_files,
        "patch_sha256": None,
        "synthetic": True,
    })
    atomic_write_json(candidate_dir / "metrics.json", evaluation.metrics)
    atomic_write_json(candidate_dir / "controls.json", evaluation.controls)
    atomic_write_json(candidate_dir / "evidence_manifest.json", {
        "contract_sha256": evaluation.contract_sha256,
        "data_eligible": evaluation.data_eligible,
        "ineligibility_reasons": evaluation.data_ineligibility_reasons,
    })
    atomic_write_json(candidate_dir / "gates.json", verdict)
    (candidate_dir / "logs").mkdir(parents=True, exist_ok=True)
    (candidate_dir / "logs" / "stdout.log").touch(exist_ok=True)
    (candidate_dir / "logs" / "stderr.log").touch(exist_ok=True)
    (candidate_dir / "patch.diff").touch(exist_ok=True)


def run_canary(contract_path: Path, repo_root: Path, run_dir: Path) -> dict[str, object]:
    repo_root = repo_root.resolve()
    contract = load_contract(contract_path)
    report = ProtocolLock(repo_root).assert_valid(contract)
    registry = ClosureRegistry.load(repo_root / contract.closure_registry)
    gate = GateEngine(contract)
    run_dir.mkdir(parents=True, exist_ok=True)
    dump_contract(contract, run_dir / "contract.locked.yaml")
    atomic_write_json(
        run_dir / "run_manifest.json",
        {
            "campaign_id": contract.campaign.id,
            "contract_sha256": report.contract_sha256,
            "base_commit": contract.campaign.base_commit,
            "canary_expectations": CANARY_EXPECTATIONS,
        },
    )
    archive = ArchiveStore(run_dir / "research.db")
    budgets = BudgetLedger(run_dir / "research.db", contract.budgets)
    results: dict[str, str] = {}

    for scenario, candidate_id, candidate, changed_files in _scenario_definitions():
        candidate_dir = run_dir / "candidates" / candidate_id
        receipt_path = candidate_dir / "reproducibility_receipt.json"
        budgets.reserve("proposal_calls", 1, f"proposal:{candidate_id}")
        budgets.reserve("mechanics_runs", 1, f"mechanics:{candidate_id}")
        if receipt_path.exists():
            envelope = load_receipt(receipt_path)
            archive.record(candidate, envelope, receipt_path.relative_to(run_dir))
            results[candidate_id] = envelope.receipt.verdict.decision.value
            continue

        raw = raw_evaluate(scenario)
        audit = audit_candidate(
            contract,
            candidate,
            registry,
            changed_files=changed_files,
        )
        evaluation = EvaluationInput(
            contract_sha256=report.contract_sha256 or "",
            candidate=candidate,
            stage=ResearchStage.H0_REAL_HEADROOM,
            changed_files=changed_files,
            protocol_violations=audit.violations,
            mechanics_status=MechanicsStatus(raw["mechanics_status"]),
            data_eligible=bool(raw["data_eligible"]),
            data_ineligibility_reasons=list(raw.get("data_ineligibility_reasons", [])),
            metrics=dict(raw["metrics"]),
            controls=dict(raw["controls"]),
            scientific_outcome=ScientificOutcome(raw["scientific_outcome"]),
        )
        started = perf_counter()
        verdict = gate.evaluate(evaluation)
        elapsed = perf_counter() - started
        _write_candidate_artifacts(candidate_dir, candidate, evaluation, verdict)
        receipt = EvaluationReceipt(
            receipt_id=f"{contract.campaign.id}:{candidate_id}",
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            campaign_id=contract.campaign.id,
            candidate_id=candidate_id,
            base_commit=contract.campaign.base_commit,
            evaluator_hashes={
                asset.asset_id: asset.sha256 or ""
                for asset in contract.frozen_assets
                if asset.kind.value == "evaluator"
            },
            data_hashes={
                source.source_id: source.sha256 or ""
                for source in contract.evidence_sources
            },
            seed=0,
            command=["synthetic-canary", scenario],
            elapsed_seconds=elapsed,
            environment=environment_receipt({"evaluator": "synthetic_canary_r0"}),
            evaluation_input=evaluation,
            verdict=verdict,
        )
        envelope = write_receipt(receipt_path, receipt)
        archive.record(candidate, envelope, receipt_path.relative_to(run_dir))
        results[candidate_id] = verdict.decision.value

    passed = results == CANARY_EXPECTATIONS
    summary = {
        "passed": passed,
        "results": results,
        "expected": CANARY_EXPECTATIONS,
        "archive": archive.summary(),
        "budgets": budgets.snapshot(),
        "summary_sha256": sha256_object(results),
    }
    atomic_write_json(run_dir / "canary_summary.json", summary)
    if not passed:
        raise RuntimeError(f"synthetic canary failed: {results}")
    return summary


def replay_run(run_dir: Path) -> dict[str, object]:
    contract = load_contract(run_dir / "contract.locked.yaml")
    gate = GateEngine(contract)
    failures: list[str] = []
    replayed = 0
    for receipt_path in sorted(
        (run_dir / "candidates").glob("*/reproducibility_receipt.json")
    ):
        envelope = load_receipt(receipt_path)
        replayed += 1
        replayed_verdict = gate.evaluate(envelope.receipt.evaluation_input)
        if replayed_verdict != envelope.receipt.verdict:
            failures.append(envelope.receipt.candidate_id)
    return {"passed": not failures, "replayed": replayed, "failures": failures}

