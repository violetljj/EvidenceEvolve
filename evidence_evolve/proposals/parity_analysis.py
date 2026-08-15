from __future__ import annotations

import math
import random
import statistics
from typing import Annotated, Literal

from pydantic import Field, model_validator

from evidence_evolve.models import ScientificOutcome, StrictModel
from evidence_evolve.proposals.non_inferiority import P2R1Protocol


Arm = Literal["official", "native"]


class ProposalSlot(StrictModel):
    slot: int = Field(ge=1, le=5)
    model_invocation_started: bool
    proposal_received: bool
    proposal_extracted: bool
    materialized: bool
    compiled: bool
    evaluator_reached: bool
    evaluator_valid: bool
    score: float | None = None

    @model_validator(mode="after")
    def stages_are_monotone(self) -> "ProposalSlot":
        stages = (
            self.model_invocation_started,
            self.proposal_received,
            self.proposal_extracted,
            self.materialized,
            self.compiled,
            self.evaluator_reached,
        )
        if any(later and not earlier for earlier, later in zip(stages, stages[1:])):
            raise ValueError("proposal funnel stages cannot skip an earlier stage")
        if self.evaluator_valid and not self.evaluator_reached:
            raise ValueError("evaluator-valid requires evaluator-reached")
        if self.evaluator_valid and (
            self.score is None or not math.isfinite(self.score)
        ):
            raise ValueError("evaluator-valid slots require a finite score")
        if not self.evaluator_valid and self.score is not None:
            raise ValueError("invalid or missing slots cannot contribute a score")
        return self


class ArmRun(StrictModel):
    block: int = Field(ge=1, le=10)
    arm: Arm
    baseline_score: float
    slots: Annotated[list[ProposalSlot], Field(min_length=5, max_length=5)]
    observed_input_tokens: int = Field(ge=0)
    observed_output_tokens: int = Field(ge=0)
    observed_cost: float | None = Field(default=None, ge=0.0)
    wall_seconds: float = Field(ge=0.0)
    resume_consistent: bool

    @model_validator(mode="after")
    def slots_are_complete_and_ordered(self) -> "ArmRun":
        if not math.isfinite(self.baseline_score):
            raise ValueError("baseline score must be finite")
        if [slot.slot for slot in self.slots] != [1, 2, 3, 4, 5]:
            raise ValueError("proposal slots must be ordered 1 through 5")
        return self


class P2R1AnalysisInput(StrictModel):
    protocol_id: Literal["SHINKA_NATIVE_P2_R1"]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runs: Annotated[list[ArmRun], Field(min_length=20, max_length=20)]

    @model_validator(mode="after")
    def matched_runs_are_complete(self) -> "P2R1AnalysisInput":
        keys = [(run.block, run.arm) for run in self.runs]
        expected = [
            (block, arm)
            for block in range(1, 11)
            for arm in ("official", "native")
        ]
        if sorted(keys) != sorted(expected):
            raise ValueError("analysis requires one run per block and arm")
        return self


class ArmFunnel(StrictModel):
    scheduled: int
    model_invocation_started: int
    proposal_received: int
    proposal_extracted: int
    materialized: int
    compiled: int
    evaluator_reached: int
    evaluator_valid: int
    evaluator_invalid: int
    useful: int


class P2R1AnalysisResult(StrictModel):
    protocol_id: Literal["SHINKA_NATIVE_P2_R1"]
    protocol_sha256: str
    statistical_eligibility: Literal["ELIGIBLE", "NOT_EVALUABLE_DATA"]
    non_inferiority_assessment: Literal[
        "NON_INFERIORITY_SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE"
    ]
    scientific_outcome: ScientificOutcome | None
    ineligibility_reasons: list[str]
    per_run_funnel: dict[str, ArmFunnel]
    per_run_funnel_rates: dict[str, dict[str, float]]
    per_arm_funnel: dict[Arm, ArmFunnel]
    per_arm_funnel_rates: dict[Arm, dict[str, float]]
    final_best_by_block: dict[str, dict[Arm, float]]
    paired_normalized_deltas: list[float]
    primary_median_delta: float
    primary_one_sided_95_percent_lower_bound: float
    evaluator_invalid_rate_by_arm: dict[Arm, float]
    evaluator_invalid_rate_delta_native_minus_official: float
    intent_to_evaluate_useful_rate_by_arm: dict[Arm, float]
    intent_to_evaluate_useful_rate_delta_native_minus_official: float
    best_so_far_auc_median_by_arm: dict[Arm, float]
    first_valid_improvement_slot_by_run: dict[str, int | None]
    evaluator_valid_scores_by_arm: dict[Arm, list[float]]
    observed_input_tokens_per_scheduled_slot_by_arm: dict[Arm, float]
    observed_output_tokens_per_scheduled_slot_by_arm: dict[Arm, float]
    observed_cost_per_scheduled_slot_by_arm: dict[Arm, float | None]
    wall_seconds_per_scheduled_slot_by_arm: dict[Arm, float]
    resume_consistent_runs_by_arm: dict[Arm, int]
    primary_gate_passed: bool
    invalid_rate_guardrail_passed: bool
    useful_rate_guardrail_passed: bool
    positive_headroom_claim_permitted: Literal[False] = False


def _funnel(runs: list[ArmRun]) -> ArmFunnel:
    slots = [slot for run in runs for slot in run.slots]
    return ArmFunnel(
        scheduled=len(slots),
        model_invocation_started=sum(slot.model_invocation_started for slot in slots),
        proposal_received=sum(slot.proposal_received for slot in slots),
        proposal_extracted=sum(slot.proposal_extracted for slot in slots),
        materialized=sum(slot.materialized for slot in slots),
        compiled=sum(slot.compiled for slot in slots),
        evaluator_reached=sum(slot.evaluator_reached for slot in slots),
        evaluator_valid=sum(slot.evaluator_valid for slot in slots),
        evaluator_invalid=sum(
            slot.evaluator_reached and not slot.evaluator_valid for slot in slots
        ),
        useful=sum(
            slot.evaluator_valid
            and slot.score is not None
            and slot.score > run.baseline_score + 1e-12
            for run in runs
            for slot in run.slots
        ),
    )


def _funnel_rates(funnel: ArmFunnel) -> dict[str, float]:
    return {
        field: value / funnel.scheduled
        for field, value in funnel.model_dump().items()
        if field != "scheduled"
    }


def _score_trajectory(run: ArmRun) -> list[float]:
    best = run.baseline_score
    trajectory = [best]
    for slot in run.slots:
        if slot.evaluator_valid and slot.score is not None:
            best = max(best, slot.score)
        trajectory.append(best)
    return trajectory


def _normalized_auc(trajectory: list[float]) -> float:
    area = sum(
        (left + right) / 2
        for left, right in zip(trajectory, trajectory[1:])
    )
    return area / (len(trajectory) - 1)


def _first_improvement_slot(run: ArmRun) -> int | None:
    return next(
        (
            slot.slot
            for slot in run.slots
            if slot.evaluator_valid
            and slot.score is not None
            and slot.score > run.baseline_score + 1e-12
        ),
        None,
    )


def _paired_bootstrap_lower_bound(
    deltas: list[float], *, resamples: int, seed: int
) -> float:
    generator = random.Random(seed)
    estimates = sorted(
        statistics.median(generator.choices(deltas, k=len(deltas)))
        for _ in range(resamples)
    )
    index = math.ceil(0.05 * resamples) - 1
    return estimates[index]


def analyze_p2_r1(
    analysis_input: P2R1AnalysisInput,
    protocol: P2R1Protocol,
) -> P2R1AnalysisResult:
    """Apply only the statistical rules frozen by the P2-R1 protocol."""

    if analysis_input.protocol_sha256 != protocol.protocol_sha256:
        raise ValueError("analysis input does not match the frozen P2-R1 protocol")

    runs_by_arm = {
        arm: [run for run in analysis_input.runs if run.arm == arm]
        for arm in ("official", "native")
    }
    funnels = {arm: _funnel(runs) for arm, runs in runs_by_arm.items()}
    funnel_rates = {arm: _funnel_rates(funnel) for arm, funnel in funnels.items()}
    per_run_funnels = {
        f"block-{run.block:02d}-{run.arm}": _funnel([run])
        for run in analysis_input.runs
    }
    per_run_funnel_rates = {
        run_id: _funnel_rates(funnel)
        for run_id, funnel in per_run_funnels.items()
    }
    trajectories = {
        f"block-{run.block:02d}-{run.arm}": _score_trajectory(run)
        for run in analysis_input.runs
    }
    final_best: dict[str, dict[Arm, float]] = {}
    deltas = []
    for block in range(1, 11):
        pair = {
            run.arm: trajectories[f"block-{run.block:02d}-{run.arm}"][-1]
            for run in analysis_input.runs
            if run.block == block
        }
        final_best[str(block)] = pair
        official = pair["official"]
        deltas.append((pair["native"] - official) / max(abs(official), 1e-12))

    median_delta = statistics.median(deltas)
    lower_bound = _paired_bootstrap_lower_bound(
        deltas,
        resamples=protocol.analysis.bootstrap.resamples,
        seed=protocol.analysis.bootstrap.seed,
    )
    invalid_rates = {
        arm: (
            funnel.evaluator_invalid / funnel.evaluator_reached
            if funnel.evaluator_reached
            else 0.0
        )
        for arm, funnel in funnels.items()
    }
    useful_rates = {
        arm: funnel.useful / funnel.scheduled for arm, funnel in funnels.items()
    }
    invalid_delta = invalid_rates["native"] - invalid_rates["official"]
    useful_delta = useful_rates["native"] - useful_rates["official"]
    auc_medians = {
        arm: statistics.median(
            _normalized_auc(trajectories[f"block-{run.block:02d}-{arm}"])
            for run in runs
        )
        for arm, runs in runs_by_arm.items()
    }
    first_improvements = {
        f"block-{run.block:02d}-{run.arm}": _first_improvement_slot(run)
        for run in analysis_input.runs
    }
    valid_scores = {
        arm: [
            slot.score
            for run in runs
            for slot in run.slots
            if slot.evaluator_valid and slot.score is not None
        ]
        for arm, runs in runs_by_arm.items()
    }
    input_tokens_per_slot = {
        arm: sum(run.observed_input_tokens for run in runs) / 50
        for arm, runs in runs_by_arm.items()
    }
    output_tokens_per_slot = {
        arm: sum(run.observed_output_tokens for run in runs) / 50
        for arm, runs in runs_by_arm.items()
    }
    cost_per_slot = {
        arm: (
            sum(run.observed_cost for run in runs if run.observed_cost is not None)
            / 50
            if all(run.observed_cost is not None for run in runs)
            else None
        )
        for arm, runs in runs_by_arm.items()
    }
    wall_per_slot = {
        arm: sum(run.wall_seconds for run in runs) / 50
        for arm, runs in runs_by_arm.items()
    }
    resume_consistency = {
        arm: sum(run.resume_consistent for run in runs)
        for arm, runs in runs_by_arm.items()
    }

    ineligibility_reasons = []
    for arm, funnel in funnels.items():
        if funnel.model_invocation_started != 50:
            ineligibility_reasons.append(f"UNEQUAL_OR_MISSING_MODEL_CALLS:{arm}")
        if funnel.evaluator_reached == 0:
            ineligibility_reasons.append(f"NO_PROPOSAL_REACHED_EVALUATOR:{arm}")
    eligible = not ineligibility_reasons
    primary_pass = lower_bound > protocol.analysis.non_inferiority_margin
    invalid_pass = invalid_delta <= 0.10
    useful_pass = useful_delta >= -0.10

    if not eligible:
        assessment = "INCONCLUSIVE"
        outcome = ScientificOutcome.NOT_EVALUABLE_DATA
        eligibility = "NOT_EVALUABLE_DATA"
    elif primary_pass and invalid_pass and useful_pass:
        assessment = "NON_INFERIORITY_SUPPORTED"
        outcome = None
        eligibility = "ELIGIBLE"
    else:
        assessment = "NOT_SUPPORTED"
        outcome = ScientificOutcome.VALID_NEGATIVE
        eligibility = "ELIGIBLE"

    return P2R1AnalysisResult(
        protocol_id=protocol.protocol_id,
        protocol_sha256=protocol.protocol_sha256,
        statistical_eligibility=eligibility,
        non_inferiority_assessment=assessment,
        scientific_outcome=outcome,
        ineligibility_reasons=sorted(ineligibility_reasons),
        per_run_funnel=per_run_funnels,
        per_run_funnel_rates=per_run_funnel_rates,
        per_arm_funnel=funnels,
        per_arm_funnel_rates=funnel_rates,
        final_best_by_block=final_best,
        paired_normalized_deltas=deltas,
        primary_median_delta=median_delta,
        primary_one_sided_95_percent_lower_bound=lower_bound,
        evaluator_invalid_rate_by_arm=invalid_rates,
        evaluator_invalid_rate_delta_native_minus_official=invalid_delta,
        intent_to_evaluate_useful_rate_by_arm=useful_rates,
        intent_to_evaluate_useful_rate_delta_native_minus_official=useful_delta,
        best_so_far_auc_median_by_arm=auc_medians,
        first_valid_improvement_slot_by_run=first_improvements,
        evaluator_valid_scores_by_arm=valid_scores,
        observed_input_tokens_per_scheduled_slot_by_arm=input_tokens_per_slot,
        observed_output_tokens_per_scheduled_slot_by_arm=output_tokens_per_slot,
        observed_cost_per_scheduled_slot_by_arm=cost_per_slot,
        wall_seconds_per_scheduled_slot_by_arm=wall_per_slot,
        resume_consistent_runs_by_arm=resume_consistency,
        primary_gate_passed=primary_pass,
        invalid_rate_guardrail_passed=invalid_pass,
        useful_rate_guardrail_passed=useful_pass,
    )
