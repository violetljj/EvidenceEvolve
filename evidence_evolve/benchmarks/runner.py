from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Callable

from evidence_evolve.artifacts import create_once_json, environment_receipt
from evidence_evolve.benchmarks.models import (
    ArmTrialSubmission,
    BenchmarkArm,
    BenchmarkArmSummary,
    BenchmarkProtocol,
    BenchmarkSuiteResult,
    BenchmarkTrialContext,
    BenchmarkTrialEnvelope,
    BenchmarkTrialReceipt,
    BenchmarkTrialRequest,
    CandidateBenchmarkEvaluation,
    SplitEvaluation,
)
from evidence_evolve.benchmarks.protocol import (
    BenchmarkProtocolLock,
    benchmark_protocol_content_hash,
)
from evidence_evolve.hashing import sha256_file, sha256_object
from tasks.graph_coloring.evaluator import evaluate_split


BenchmarkArmAdapter = Callable[[BenchmarkTrialContext], ArmTrialSubmission]


def _create_or_validate(path: Path, payload: object) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        expected = (
            payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
        if existing != expected:
            raise ValueError(f"immutable benchmark artifact drift: {path}")
        return
    create_once_json(path, payload)


def _load_trial_envelope(path: Path) -> BenchmarkTrialEnvelope:
    envelope = BenchmarkTrialEnvelope.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    actual = sha256_object(envelope.receipt)
    if actual != envelope.receipt_sha256:
        raise ValueError(f"benchmark trial receipt hash mismatch: {path}")
    return envelope


class ThreeArmBenchmarkRunner:
    """Execute a paired, equal-ceiling three-arm benchmark protocol.

    Candidate selection consumes only the development split. Public fresh scores
    are computed after selection. Because those instances and this evaluator are
    public, every output remains explicitly non-blind and non-promotional.
    """

    def __init__(
        self,
        *,
        protocol: BenchmarkProtocol,
        repo_root: Path,
        run_dir: Path,
        adapter: BenchmarkArmAdapter,
    ) -> None:
        self.protocol = protocol
        self.repo_root = repo_root.resolve()
        self.run_dir = run_dir.resolve()
        self.adapter = adapter
        BenchmarkProtocolLock(self.repo_root).assert_valid(protocol)
        if protocol.lock is None:  # pragma: no cover - assert_valid guards this
            raise ValueError("benchmark protocol must be locked")
        self.protocol_sha256 = benchmark_protocol_content_hash(protocol)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _create_or_validate(
            self.run_dir / "run_manifest.json",
            {
                "suite_id": protocol.suite_id,
                "protocol_sha256": self.protocol_sha256,
                "arms": [arm.value for arm in protocol.arms],
                "trial_seeds": protocol.trial_seeds,
                "claim_scope": protocol.claim_scope,
                "blind_confirmation_available": False,
            },
        )

    def _candidate_path(self, value: str, trial_dir: Path) -> Path:
        raw = Path(value)
        candidate = raw.resolve() if raw.is_absolute() else (self.repo_root / raw).resolve()
        allowed_roots = (self.repo_root, trial_dir.resolve())
        if not any(
            candidate == root or root in candidate.parents for root in allowed_roots
        ):
            raise ValueError(f"candidate path is outside allowed roots: {value}")
        if not candidate.is_file():
            raise FileNotFoundError(f"candidate solver is not a file: {value}")
        return candidate

    def _run_trial(self, arm: BenchmarkArm, trial_seed: int) -> BenchmarkTrialEnvelope:
        trial_dir = self.run_dir / "trials" / arm.value / str(trial_seed)
        receipt_path = trial_dir / "receipt.json"
        if receipt_path.exists():
            envelope = _load_trial_envelope(receipt_path)
            receipt = envelope.receipt
            if (
                receipt.protocol_sha256 != self.protocol_sha256
                or receipt.arm != arm
                or receipt.trial_seed != trial_seed
            ):
                raise ValueError(f"benchmark trial identity drift: {receipt_path}")
            return envelope

        request = BenchmarkTrialRequest(
            suite_id=self.protocol.suite_id,
            protocol_sha256=self.protocol_sha256,
            arm=arm,
            trial_seed=trial_seed,
            proposal_call_limit=self.protocol.budget.proposal_calls_per_trial,
            candidate_evaluation_limit=(
                self.protocol.budget.candidate_evaluations_per_trial
            ),
            token_limit=self.protocol.budget.token_limit_per_trial,
            development_instance_ids=[
                item.instance_id for item in self.protocol.development.instances
            ],
        )
        _create_or_validate(trial_dir / "request.json", request)
        context = BenchmarkTrialContext(
            suite_id=self.protocol.suite_id,
            protocol_sha256=self.protocol_sha256,
            arm=arm,
            trial_seed=trial_seed,
            repo_root=self.repo_root,
            trial_dir=trial_dir,
            budget=self.protocol.budget,
            development_instances=tuple(self.protocol.development.instances),
        )
        started = perf_counter()
        submission = ArmTrialSubmission.model_validate(self.adapter(context))
        if (
            submission.proposal_calls_used
            > self.protocol.budget.proposal_calls_per_trial
        ):
            raise ValueError(f"proposal budget exceeded for {arm.value}:{trial_seed}")
        if submission.token_count_used > self.protocol.budget.token_limit_per_trial:
            raise ValueError(f"token budget exceeded for {arm.value}:{trial_seed}")
        if (
            len(submission.candidate_paths)
            > self.protocol.budget.candidate_evaluations_per_trial
        ):
            raise ValueError(
                f"candidate evaluation budget exceeded for {arm.value}:{trial_seed}"
            )
        _create_or_validate(trial_dir / "submission.json", submission)

        development_evaluations: list[
            tuple[str, str, Path, str, SplitEvaluation]
        ] = []
        for index, candidate_value in enumerate(submission.candidate_paths, start=1):
            candidate_path = self._candidate_path(candidate_value, trial_dir)
            development = evaluate_split(
                candidate_path,
                self.protocol.development.instances,
                visibility=self.protocol.development.visibility,
                trial_seed=trial_seed,
            )
            development_evaluations.append(
                (
                    f"C{index:03d}",
                    candidate_value.replace("\\", "/"),
                    candidate_path,
                    sha256_file(candidate_path),
                    development,
                )
            )

        selected_candidate_id = max(
            development_evaluations,
            key=lambda item: (
                item[4].valid_rate,
                item[4].reproducibility_rate,
                item[4].mean_relative_improvement,
                -int(item[0][1:]),
            ),
        )[0]
        evaluations: list[CandidateBenchmarkEvaluation] = []
        for (
            candidate_id,
            candidate_value,
            candidate_path,
            candidate_sha256,
            development,
        ) in development_evaluations:
            public_fresh = evaluate_split(
                candidate_path,
                self.protocol.public_fresh.instances,
                visibility=self.protocol.public_fresh.visibility,
                trial_seed=trial_seed,
            )
            evaluations.append(
                CandidateBenchmarkEvaluation(
                    candidate_id=candidate_id,
                    candidate_path=candidate_value,
                    candidate_sha256=candidate_sha256,
                    development=development,
                    public_fresh=public_fresh,
                )
            )
        wall_seconds_used = perf_counter() - started
        if wall_seconds_used > self.protocol.budget.wall_seconds_per_trial:
            raise ValueError(f"wall-time budget exceeded for {arm.value}:{trial_seed}")

        selected = next(
            item for item in evaluations if item.candidate_id == selected_candidate_id
        )
        receipt = BenchmarkTrialReceipt(
            suite_id=self.protocol.suite_id,
            protocol_sha256=self.protocol_sha256,
            arm=arm,
            trial_seed=trial_seed,
            executor_id=submission.executor_id,
            proposal_calls_used=submission.proposal_calls_used,
            candidate_evaluations_used=len(evaluations),
            token_count_used=submission.token_count_used,
            wall_seconds_used=wall_seconds_used,
            environment=environment_receipt(
                {"benchmark_executor": submission.executor_id}
            ),
            selected_candidate_id=selected.candidate_id,
            selected_candidate_sha256=selected.candidate_sha256,
            selected_development=selected.development,
            selected_public_fresh=selected.public_fresh,
            candidate_evaluations=evaluations,
        )
        envelope = BenchmarkTrialEnvelope(
            receipt=receipt,
            receipt_sha256=sha256_object(receipt),
        )
        create_once_json(receipt_path, envelope)
        return envelope

    @staticmethod
    def _summarize(
        arm: BenchmarkArm,
        receipts: list[BenchmarkTrialReceipt],
    ) -> BenchmarkArmSummary:
        trial_count = len(receipts)
        evaluations = sum(item.candidate_evaluations_used for item in receipts)
        fresh_positive = sum(
            item.selected_public_fresh.positive_relative_improvement
            for item in receipts
        )
        all_evaluations = [
            evaluation
            for receipt in receipts
            for evaluation in receipt.candidate_evaluations
        ]
        duplicate_count = sum(
            sum(count - 1 for count in Counter(
                evaluation.candidate_sha256
                for evaluation in receipt.candidate_evaluations
            ).values())
            for receipt in receipts
        )
        return BenchmarkArmSummary(
            arm=arm,
            trial_count=trial_count,
            valid_public_fresh_improvement_per_cost=(fresh_positive / evaluations),
            mean_public_fresh_improvement=sum(
                item.selected_public_fresh.mean_relative_improvement
                for item in receipts
            )
            / trial_count,
            mean_development_improvement=sum(
                item.selected_development.mean_relative_improvement
                for item in receipts
            )
            / trial_count,
            public_fresh_valid_rate=sum(
                item.selected_public_fresh.valid_rate for item in receipts
            )
            / trial_count,
            reproducibility_rate=sum(
                item.selected_public_fresh.reproducibility_rate for item in receipts
            )
            / trial_count,
            invalid_candidate_rate=sum(
                1.0 - item.development.valid_rate for item in all_evaluations
            )
            / len(all_evaluations),
            redundant_candidate_rate=duplicate_count / len(all_evaluations),
            proposal_calls_used=sum(item.proposal_calls_used for item in receipts),
            candidate_evaluations_used=evaluations,
            token_count_used=sum(item.token_count_used for item in receipts),
            wall_seconds_used=sum(item.wall_seconds_used for item in receipts),
        )

    def run(self) -> BenchmarkSuiteResult:
        receipts_by_arm: dict[BenchmarkArm, list[BenchmarkTrialReceipt]] = {}
        for arm in self.protocol.arms:
            receipts_by_arm[arm] = [
                self._run_trial(arm, seed).receipt
                for seed in self.protocol.trial_seeds
            ]
        summaries = [
            self._summarize(arm, receipts_by_arm[arm])
            for arm in self.protocol.arms
        ]
        vanilla = next(
            item for item in summaries if item.arm == BenchmarkArm.VANILLA_CODEX
        )
        deltas = {
            item.arm.value: (
                item.valid_public_fresh_improvement_per_cost
                - vanilla.valid_public_fresh_improvement_per_cost
            )
            for item in summaries
            if item.arm != BenchmarkArm.VANILLA_CODEX
        }
        result = BenchmarkSuiteResult(
            suite_id=self.protocol.suite_id,
            protocol_sha256=self.protocol_sha256,
            arms=summaries,
            paired_primary_deltas_vs_vanilla=deltas,
            reasons=[
                "PUBLIC_FRESH_SET_IS_VISIBLE_IN_REPOSITORY",
                "EXTERNAL_BLIND_CONFIRMATION_NOT_CONFIGURED",
                "PROTOCOL_SMOKE_CANNOT_ESTABLISH_RESEARCH_SUPERIORITY",
            ],
        )
        _create_or_validate(self.run_dir / "suite_result.json", result)
        return result
