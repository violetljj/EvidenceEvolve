from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.models import ScientificOutcome, StrictModel


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Arm = Literal["official", "native"]


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("frozen artifact paths must be repository-relative")
    return normalized


class FrozenBinding(StrictModel):
    path: str
    sha256: Sha256

    _normalize_path = field_validator("path")(_relative_path)


class LineageBinding(StrictModel):
    run_id: str
    relationship: Literal["PREDECESSOR", "MECHANICS_ADMISSION"]
    disposition: Literal["CLOSED_NOT_EVALUABLE", "MECHANICS_PASS"]
    scientific_outcome: ScientificOutcome | None
    scientific_outcome_authority: Literal["SCIENTIFIC", "NONE"]
    artifact: FrozenBinding


class UpstreamBinding(StrictModel):
    repository: str
    distribution: Literal["shinka-evolve"]
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    version: Literal["0.0.7"]


class ProviderPolicy(StrictModel):
    transport: str
    model: Literal["gpt-5.6-terra"]
    forbidden_models: list[str]
    reasoning_effort: Literal["high"]
    temperature: Literal[0.0]
    max_output_tokens_per_call: Literal[32768]
    model_generation_seed: None
    model_seed_gap: str
    retry_failed_model_call: Literal[False]

    @field_validator("forbidden_models")
    @classmethod
    def gpt_55_is_forbidden(cls, values: list[str]) -> list[str]:
        if "gpt-5.5" not in values:
            raise ValueError("gpt-5.5 must remain forbidden")
        if "gpt-5.6-terra" in values:
            raise ValueError("the frozen generation model cannot be forbidden")
        return values


class ArmBudget(StrictModel):
    proposal_model_calls: Literal[50]
    max_output_tokens_per_call: Literal[32768]
    max_output_tokens_total: Literal[1638400]
    wall_seconds_per_run: Literal[1800]
    wall_seconds_total: Literal[18000]
    generation_slots_including_baseline_per_run: Literal[6]
    proposal_slots_per_run: Literal[5]
    matched_runs: Literal[10]


class MatchedBlock(StrictModel):
    block: int = Field(ge=1, le=10)
    local_seed: int = Field(ge=2026081501, le=2026081510)
    order: tuple[Arm, Arm]

    @model_validator(mode="after")
    def contains_each_arm_once(self) -> "MatchedBlock":
        if set(self.order) != {"official", "native"}:
            raise ValueError("each matched block must contain each arm once")
        if self.local_seed != 2026081500 + self.block:
            raise ValueError("matched block seed does not follow the frozen schedule")
        expected = (
            ("official", "native")
            if self.block % 2
            else ("native", "official")
        )
        if self.order != expected:
            raise ValueError("matched block order does not follow the frozen AB/BA schedule")
        return self


class DesignPolicy(StrictModel):
    arms: tuple[Arm, Arm]
    matched_blocks: Literal[10]
    proposal_slots_per_run: Literal[5]
    proposal_slots_per_arm: Literal[50]
    arm_budgets: dict[Arm, ArmBudget]
    schedule: Annotated[list[MatchedBlock], Field(min_length=10, max_length=10)]
    parent_selection_strategy: Literal["weighted"]
    archive_selection_strategy: Literal["fitness"]
    proposal_target_mode: Literal["fixed"]
    initial_parent_identical_within_block: Literal[True]
    local_seed_identical_within_block: Literal[True]
    no_post_hoc_replacement: Literal[True]
    failed_or_invalid_slot_consumes_budget: Literal[True]
    resume_may_add_proposal_slots: Literal[False]

    @model_validator(mode="after")
    def arms_budgets_and_schedule_are_symmetric(self) -> "DesignPolicy":
        if self.arms != ("official", "native"):
            raise ValueError("arms must be frozen in official/native order")
        if set(self.arm_budgets) != {"official", "native"}:
            raise ValueError("both arm budgets are required")
        if self.arm_budgets["official"] != self.arm_budgets["native"]:
            raise ValueError("arm budgets must be identical")
        if [block.block for block in self.schedule] != list(range(1, 11)):
            raise ValueError("matched blocks must be ordered 1 through 10")
        return self


class FunnelPolicy(StrictModel):
    denominator: Literal["SCHEDULED_PROPOSAL_SLOTS"]
    stages: tuple[
        Literal["scheduled"],
        Literal["model_invocation_started"],
        Literal["proposal_received"],
        Literal["proposal_extracted"],
        Literal["materialized"],
        Literal["compiled"],
        Literal["evaluator_reached"],
        Literal["evaluator_valid"],
        Literal["useful"],
    ]
    report_by: tuple[Literal["run"], Literal["arm"], Literal["overall"]]
    report_counts_and_rates: Literal[True]
    baseline_evaluations_excluded_from_proposal_funnel: Literal[True]


class InvalidHandlingPolicy(StrictModel):
    evaluator_invalid_remains_in_primary_denominator: Literal[True]
    pre_evaluator_failure_remains_in_primary_denominator: Literal[True]
    missing_response_remains_in_primary_denominator: Literal[True]
    invalid_or_missing_slot_score_rule: Literal["CARRY_FORWARD_PREVIOUS_BEST"]
    baseline_initializes_score_trajectory: Literal[True]
    evaluator_valid_subset_is_descriptive_only: Literal[True]
    useful_candidate_definition: Literal[
        "EVALUATOR_VALID_AND_SCORE_GT_FROZEN_BASELINE_PLUS_1E-12"
    ]
    evaluator_invalid_rate_denominator: Literal["EVALUATOR_REACHED"]
    intent_to_evaluate_success_denominator: Literal["SCHEDULED_PROPOSAL_SLOTS"]
    no_valid_only_resampling: Literal[True]


class BootstrapPolicy(StrictModel):
    method: Literal["PAIRED_BLOCK_BOOTSTRAP"]
    resamples: Literal[100000]
    seed: Literal[2026081599]
    confidence: Literal[0.95]
    side: Literal["LOWER_ONE_SIDED"]


class Guardrail(StrictModel):
    metric: Literal[
        "evaluator_invalid_rate_delta_native_minus_official",
        "intent_to_evaluate_useful_rate_delta_native_minus_official",
    ]
    min: float | None = None
    max: float | None = None
    margin_unit: Literal["ABSOLUTE_RATE"]

    @model_validator(mode="after")
    def has_exactly_one_bound(self) -> "Guardrail":
        if (self.min is None) == (self.max is None):
            raise ValueError("a guardrail must have exactly one bound")
        return self


class AnalysisPolicy(StrictModel):
    primary_metric: Literal["PAIRED_NORMALIZED_FINAL_BEST_SCORE_DELTA"]
    per_block_formula: Literal[
        "(native_final_best-official_final_best)/max(abs(official_final_best),1e-12)"
    ]
    aggregation: Literal["MEDIAN_ACROSS_MATCHED_BLOCKS"]
    non_inferiority_margin: Literal[-0.01]
    pass_rule: Literal[
        "ONE_SIDED_95_PERCENT_LOWER_BOUND_STRICTLY_GT_MARGIN_AND_ALL_GUARDRAILS_PASS"
    ]
    bootstrap: BootstrapPolicy
    ties: Literal["DELTA_ZERO"]
    minimum_complete_matched_blocks: Literal[10]
    equal_started_model_calls_per_arm_required: Literal[True]
    scheduled_model_calls_per_arm_required: Literal[50]
    hard_guardrails: Annotated[list[Guardrail], Field(min_length=2, max_length=2)]
    mandatory_secondary_metrics: list[str]
    aggregate_score_cannot_rescue_guardrail: Literal[True]

    @model_validator(mode="after")
    def guardrails_are_frozen(self) -> "AnalysisPolicy":
        by_metric = {guardrail.metric: guardrail for guardrail in self.hard_guardrails}
        if set(by_metric) != {
            "evaluator_invalid_rate_delta_native_minus_official",
            "intent_to_evaluate_useful_rate_delta_native_minus_official",
        }:
            raise ValueError("both frozen invalid/useful-rate guardrails are required")
        invalid = by_metric["evaluator_invalid_rate_delta_native_minus_official"]
        useful = by_metric[
            "intent_to_evaluate_useful_rate_delta_native_minus_official"
        ]
        if invalid.max != 0.10 or invalid.min is not None:
            raise ValueError("invalid-rate guardrail must be <= +0.10")
        if useful.min != -0.10 or useful.max is not None:
            raise ValueError("useful-rate guardrail must be >= -0.10")
        return self


class OutcomePolicy(StrictModel):
    supported_assessment: Literal["NON_INFERIORITY_SUPPORTED"]
    supported_claim_ceiling: Literal["PARITY_ONLY"]
    positive_headroom_claim_permitted: Literal[False]
    eligible_gate_failure: Literal[ScientificOutcome.VALID_NEGATIVE]
    missing_eligible_truth: Literal[ScientificOutcome.NOT_EVALUABLE_DATA]
    mechanics_or_protocol_failure: Literal[
        ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
    ]
    unknown_is_negative: Literal[False]


class P2R1Protocol(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Literal["SHINKA_NATIVE_P2_R1"]
    protocol_status: Literal["FROZEN_NOT_STARTED"]
    execution_started: Literal[False]
    remote_model_calls_at_freeze: Literal[0]
    protocol_sha256: Sha256
    claim_scope: Literal["REAL_PROVIDER_MATCHED_BLOCK_NON_INFERIORITY"]
    lineage: Annotated[list[LineageBinding], Field(min_length=2, max_length=2)]
    upstream: UpstreamBinding
    frozen_assets: dict[str, FrozenBinding]
    provider: ProviderPolicy
    design: DesignPolicy
    funnel: FunnelPolicy
    invalid_handling: InvalidHandlingPolicy
    analysis: AnalysisPolicy
    outcome_policy: OutcomePolicy

    @model_validator(mode="after")
    def lineage_and_assets_are_complete(self) -> "P2R1Protocol":
        if [item.run_id for item in self.lineage] != [
            "SHINKA_NATIVE_P2_R0",
            "SHINKA_NATIVE_P2_M0_R0",
        ]:
            raise ValueError("P2-R1 lineage order must be P2-R0 then P2-M0")
        lineage = {item.run_id: item for item in self.lineage}
        if set(lineage) != {"SHINKA_NATIVE_P2_R0", "SHINKA_NATIVE_P2_M0_R0"}:
            raise ValueError("P2-R1 must bind exactly P2-R0 and P2-M0")
        r0 = lineage["SHINKA_NATIVE_P2_R0"]
        if not (
            r0.relationship == "PREDECESSOR"
            and r0.disposition == "CLOSED_NOT_EVALUABLE"
            and r0.scientific_outcome is ScientificOutcome.NOT_EVALUABLE_DATA
            and r0.scientific_outcome_authority == "SCIENTIFIC"
        ):
            raise ValueError("P2-R0 lineage semantics changed")
        m0 = lineage["SHINKA_NATIVE_P2_M0_R0"]
        if not (
            m0.relationship == "MECHANICS_ADMISSION"
            and m0.disposition == "MECHANICS_PASS"
            and m0.scientific_outcome is None
            and m0.scientific_outcome_authority == "NONE"
        ):
            raise ValueError("P2-M0 lineage semantics changed")
        required_assets = {
            "config",
            "initial_program",
            "evaluator",
            "seed_layer",
            "materializer",
            "shinka_adapter",
            "native_engine",
            "protocol_validator",
            "statistical_analyzer",
        }
        if set(self.frozen_assets) != required_assets:
            raise ValueError("P2-R1 frozen asset set is incomplete or expanded")
        return self


def load_and_validate_p2_r1_protocol(
    protocol_path: Path,
    *,
    repo: Path,
) -> P2R1Protocol:
    """Validate the frozen P2-R1 protocol without starting an experiment."""

    raw = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol = P2R1Protocol.model_validate(raw)
    hash_payload = dict(raw)
    declared_hash = hash_payload.pop("protocol_sha256")
    if sha256_object(hash_payload) != declared_hash:
        raise ValueError("P2-R1 protocol hash mismatch")

    bindings = [item.artifact for item in protocol.lineage]
    bindings.extend(protocol.frozen_assets.values())
    for binding in bindings:
        path = repo / binding.path
        if not path.is_file() or sha256_file(path) != binding.sha256:
            raise ValueError(f"P2-R1 frozen artifact hash mismatch: {binding.path}")

    config_path = repo / protocol.frozen_assets["config"].path
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    expected_model = (
        f"headless/codex@{protocol.provider.model}"
        f"?effort={protocol.provider.reasoning_effort}"
    )
    evo = config["evo_config"]
    if evo["llm_models"] != [expected_model]:
        raise ValueError("P2-R1 config model does not match protocol")
    if evo["llm_kwargs"] != {
        "temperatures": [protocol.provider.temperature],
        "max_tokens": protocol.provider.max_output_tokens_per_call,
    }:
        raise ValueError("P2-R1 config generation limits do not match protocol")
    if evo["patch_types"] != ["diff"] or evo["patch_type_probs"] != [1.0]:
        raise ValueError("P2-R1 requires the admitted SEARCH/REPLACE proposal dialect")
    if config["job_config"]["time"] != "00:30:00":
        raise ValueError("P2-R1 run time budget does not match protocol")
    if any(
        config[name] != 1
        for name in ("max_evaluation_jobs", "max_proposal_jobs", "max_db_workers")
    ):
        raise ValueError("P2-R1 worker policy does not match protocol")

    r0 = json.loads((repo / protocol.lineage[0].artifact.path).read_text())
    if not (
        r0["campaign_state"] == "CLOSED_NOT_EVALUABLE"
        and r0["scientific_outcome"] == "NOT_EVALUABLE_DATA"
    ):
        raise ValueError("P2-R0 closure no longer preserves NOT_EVALUABLE_DATA")
    m0 = json.loads((repo / protocol.lineage[1].artifact.path).read_text())
    if not (
        m0["mechanics_status"] == "PASS"
        and m0["scientific_outcome_authority"] == "NONE"
        and m0["frozen_sample"]["remote_model_calls"] == 0
    ):
        raise ValueError("P2-M0 no longer provides mechanics-only admission")
    return protocol
