from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.artifacts import (
    create_once_json,
    environment_receipt,
    load_receipt,
    write_receipt,
)
from evidence_evolve.budgets import BudgetLedger
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.gate_engine import GateEngine
from evidence_evolve.meta_evolution.policy import (
    AcquisitionDecision,
    CandidateAcquisition,
    ResearchPolicyGenome,
    rank_candidates,
)
from evidence_evolve.models import (
    EvaluationInput,
    EvaluationReceipt,
    FrozenAssetKind,
    GateVerdict,
    ResearchContract,
    ResearchStage,
    StrictModel,
)
from evidence_evolve.understanding.signatures import (
    MechanismAssessment,
    assess_mechanism,
)


class CampaignCandidate(StrictModel):
    acquisition: CandidateAcquisition
    stage: ResearchStage = ResearchStage.M0_MECHANICS
    reference_metrics: dict[str, float] = Field(default_factory=dict)


class EvaluationRun(StrictModel):
    evaluation: EvaluationInput
    command: list[str]
    elapsed_seconds: float = Field(ge=0.0)
    seed: int = 0
    candidate_commit: str | None = None
    patch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ablation_results: dict[str, bool] = Field(default_factory=dict)


class CandidateRunResult(StrictModel):
    candidate_id: str
    receipt_path: str
    verdict: GateVerdict
    mechanism: MechanismAssessment | None = None
    resumed: bool = False


class CampaignGenerationResult(StrictModel):
    generation_id: str
    policy_id: str
    decisions: list[AcquisitionDecision]
    evaluations: list[CandidateRunResult] = Field(default_factory=list)


EvaluationAdapter = Callable[[CampaignCandidate], EvaluationRun]


_STAGE_BUDGET = {
    ResearchStage.M0_MECHANICS: "mechanics_runs",
    ResearchStage.H0_REAL_HEADROOM: "proxy_runs",
    ResearchStage.T0_LEARNED_CANDIDATE: "proxy_runs",
    ResearchStage.C0_CONFIRMATION: "confirmation_runs",
    ResearchStage.D0_DEPLOYMENT: "device_runs",
}


class CampaignRunner:
    """Schedule and ingest one generation without owning proposal or fitness truth.

    The adapter is task-specific and supplies frozen observations. This runner
    alone derives the gate verdict, writes the immutable receipt, and stores the
    scheduling-only mechanism assessment.
    """

    def __init__(
        self,
        *,
        contract: ResearchContract,
        closure_registry: ClosureRegistry,
        policy: ResearchPolicyGenome,
        run_dir: Path,
    ) -> None:
        if contract.lock is None:
            raise ValueError("campaign runner requires a locked research contract")
        self.contract = contract
        self.closure_registry = closure_registry
        self.policy = policy
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.run_dir / "research.db"
        self.archive = ArchiveStore(self.database)
        self.budgets = BudgetLedger(self.database, contract.budgets)

    def run_generation(
        self,
        *,
        generation_id: str,
        candidates: list[CampaignCandidate],
        evaluate: EvaluationAdapter,
        max_evaluations: int | None = None,
        signature_tolerance: float = 0.0,
    ) -> CampaignGenerationResult:
        if not candidates:
            raise ValueError("generation candidate pool cannot be empty")
        if max_evaluations is not None and max_evaluations < 0:
            raise ValueError("max_evaluations must be non-negative")

        pool = [item.acquisition for item in candidates]
        decisions = rank_candidates(
            policy=self.policy,
            candidates=pool,
            closure_registry=self.closure_registry,
        )
        by_id = {
            item.acquisition.candidate.candidate_id: item for item in candidates
        }
        for candidate_id in by_id:
            self.budgets.reserve(
                "proposal_calls", 1, f"proposal_calls:{generation_id}:{candidate_id}"
            )
        created_at = datetime.now(timezone.utc).isoformat()
        self.archive.record_acquisition(
            generation_id=generation_id,
            policy_id=self.policy.policy_id,
            pool=pool,
            decisions=decisions,
            created_at_utc=created_at,
        )
        selected = [decision for decision in decisions if decision.eligible]
        if max_evaluations is not None:
            selected = selected[:max_evaluations]

        results: list[CandidateRunResult] = []
        for decision in selected:
            item = by_id[decision.candidate_id]
            results.append(
                self._evaluate_candidate(
                    generation_id=generation_id,
                    item=item,
                    evaluate=evaluate,
                    signature_tolerance=signature_tolerance,
                )
            )
        return CampaignGenerationResult(
            generation_id=generation_id,
            policy_id=self.policy.policy_id,
            decisions=decisions,
            evaluations=results,
        )

    def _evaluate_candidate(
        self,
        *,
        generation_id: str,
        item: CampaignCandidate,
        evaluate: EvaluationAdapter,
        signature_tolerance: float,
    ) -> CandidateRunResult:
        candidate = item.acquisition.candidate
        budget_category = _STAGE_BUDGET.get(item.stage)
        if budget_category is None:
            raise ValueError(f"stage is not executable by discovery runner: {item.stage}")
        self.budgets.reserve(
            budget_category,
            1,
            f"{budget_category}:{generation_id}:{candidate.candidate_id}",
        )
        receipt_dir = self.run_dir / "candidates" / candidate.candidate_id / "receipts"
        receipt_path = receipt_dir / f"{generation_id}.{item.stage.value}.json"
        relative_receipt = receipt_path.relative_to(self.run_dir)
        if receipt_path.exists():
            envelope = load_receipt(receipt_path)
            if envelope.receipt.candidate_id != candidate.candidate_id:
                raise ValueError("existing receipt candidate mismatch")
            self.archive.record(candidate, envelope, relative_receipt)
            mechanism_path = receipt_path.with_suffix(".mechanism.json")
            mechanism = None
            if mechanism_path.exists():
                mechanism = MechanismAssessment.model_validate_json(
                    mechanism_path.read_text(encoding="utf-8")
                )
                self.archive.record_mechanism_assessment(
                    receipt_id=envelope.receipt.receipt_id,
                    assessment=mechanism,
                    created_at_utc=envelope.receipt.created_at_utc,
                )
            return CandidateRunResult(
                candidate_id=candidate.candidate_id,
                receipt_path=relative_receipt.as_posix(),
                verdict=envelope.receipt.verdict,
                mechanism=mechanism,
                resumed=True,
            )

        run = evaluate(item)
        evaluation = run.evaluation
        if evaluation.candidate.candidate_id != candidate.candidate_id:
            raise ValueError("adapter returned an evaluation for a different candidate")
        if evaluation.stage is not item.stage:
            raise ValueError("adapter returned an evaluation for a different stage")
        if evaluation.contract_sha256 != self.contract.lock.content_sha256:
            raise ValueError("adapter evaluation is bound to a different contract")

        audit = audit_candidate(
            self.contract,
            candidate,
            self.closure_registry,
            changed_files=evaluation.changed_files,
            verified_reopen_conditions=item.acquisition.verified_reopen_conditions,
        )
        evaluation = evaluation.model_copy(
            update={
                "protocol_violations": sorted(
                    set(evaluation.protocol_violations) | set(audit.violations)
                )
            }
        )

        verdict = GateEngine(self.contract).evaluate(evaluation)
        receipt_id = (
            f"{self.contract.campaign.id}:{generation_id}:"
            f"{candidate.candidate_id}:{item.stage.value}"
        )
        receipt = EvaluationReceipt(
            receipt_id=receipt_id,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            campaign_id=self.contract.campaign.id,
            candidate_id=candidate.candidate_id,
            base_commit=self.contract.campaign.base_commit,
            candidate_commit=run.candidate_commit,
            patch_sha256=run.patch_sha256,
            evaluator_hashes={
                asset.asset_id: asset.sha256 or ""
                for asset in self.contract.frozen_assets
                if asset.kind is FrozenAssetKind.EVALUATOR
            },
            data_hashes={
                source.source_id: source.sha256 or ""
                for source in self.contract.evidence_sources
            },
            seed=run.seed,
            command=run.command,
            elapsed_seconds=run.elapsed_seconds,
            environment=environment_receipt(
                {
                    "component": "R1_CAMPAIGN_RUNNER",
                    "policy_id": self.policy.policy_id,
                }
            ),
            evaluation_input=evaluation,
            verdict=verdict,
        )
        envelope = write_receipt(receipt_path, receipt)
        self.archive.record(candidate, envelope, relative_receipt)
        mechanism = assess_mechanism(
            contract=self.contract,
            candidate=candidate,
            evaluation=evaluation,
            verdict=verdict,
            reference_metrics=item.reference_metrics,
            ablation_results=run.ablation_results,
            tolerance=signature_tolerance,
        )
        mechanism_path = receipt_path.with_suffix(".mechanism.json")
        create_once_json(mechanism_path, mechanism)
        self.archive.record_mechanism_assessment(
            receipt_id=receipt_id,
            assessment=mechanism,
            created_at_utc=receipt.created_at_utc,
        )
        return CandidateRunResult(
            candidate_id=candidate.candidate_id,
            receipt_path=relative_receipt.as_posix(),
            verdict=verdict,
            mechanism=mechanism,
        )
