from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from evidence_evolve.models import EnvironmentReceipt, StrictModel


class BenchmarkArm(StrEnum):
    VANILLA_CODEX = "VANILLA_CODEX"
    EVIDENCE_EVOLVE_NO_MEMORY = "EVIDENCE_EVOLVE_NO_MEMORY"
    EVIDENCE_EVOLVE_FULL = "EVIDENCE_EVOLVE_FULL"


THREE_ARM_SET = frozenset(BenchmarkArm)


class DatasetVisibility(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    PUBLIC_FRESH = "PUBLIC_FRESH"
    EXTERNAL_BLIND_CONFIRMATION = "EXTERNAL_BLIND_CONFIRMATION"


class BenchmarkAsset(StrictModel):
    asset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    path: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def relative_repository_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts or normalized.startswith("/"):
            raise ValueError("benchmark asset path must stay inside the repository")
        return normalized


class GraphInstanceSpec(StrictModel):
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    seed: int = Field(ge=0)
    node_count: int = Field(ge=8, le=500)
    edge_probability: float = Field(gt=0.0, lt=1.0)


class GraphDatasetSpec(StrictModel):
    visibility: DatasetVisibility
    instances: list[GraphInstanceSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_instances(self) -> "GraphDatasetSpec":
        ids = [item.instance_id for item in self.instances]
        if len(ids) != len(set(ids)):
            raise ValueError("graph instance ids must be unique within a split")
        return self


class BenchmarkBudget(StrictModel):
    proposal_calls_per_trial: int = Field(ge=0)
    candidate_evaluations_per_trial: int = Field(ge=1)
    token_limit_per_trial: int = Field(ge=0)
    wall_seconds_per_trial: float = Field(gt=0.0)


class BenchmarkStopRule(StrictModel):
    complete_all_arms_and_seeds: Literal[True] = True
    early_superiority_stop_allowed: Literal[False] = False
    paired_seed_comparison: Literal[True] = True


class BenchmarkProtocolLockData(StrictModel):
    algorithm: Literal["sha256"] = "sha256"
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BenchmarkProtocol(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    task_id: Literal["graph_coloring"] = "graph_coloring"
    claim_scope: Literal["BENCHMARK_PROTOCOL_SMOKE_ONLY"] = (
        "BENCHMARK_PROTOCOL_SMOKE_ONLY"
    )
    arm_adapter: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
    )
    arms: list[BenchmarkArm]
    trial_seeds: list[int] = Field(min_length=1)
    budget: BenchmarkBudget
    development: GraphDatasetSpec
    public_fresh: GraphDatasetSpec
    blind_confirmation_available: Literal[False] = False
    selection_split: Literal["DEVELOPMENT"] = "DEVELOPMENT"
    primary_metric: Literal["valid_public_fresh_improvement_per_cost"] = (
        "valid_public_fresh_improvement_per_cost"
    )
    secondary_metrics: list[str] = Field(min_length=1)
    stop_rule: BenchmarkStopRule
    assets: list[BenchmarkAsset] = Field(min_length=1)
    lock: BenchmarkProtocolLockData | None = None

    @model_validator(mode="after")
    def protocol_invariants(self) -> "BenchmarkProtocol":
        if len(self.arms) != len(set(self.arms)) or set(self.arms) != THREE_ARM_SET:
            raise ValueError("benchmark must contain each frozen three-arm comparator once")
        if len(self.trial_seeds) != len(set(self.trial_seeds)):
            raise ValueError("trial seeds must be unique")
        if self.development.visibility is not DatasetVisibility.DEVELOPMENT:
            raise ValueError("development split visibility must be DEVELOPMENT")
        if self.public_fresh.visibility is not DatasetVisibility.PUBLIC_FRESH:
            raise ValueError("fresh split must be explicitly PUBLIC_FRESH")
        development_ids = {item.instance_id for item in self.development.instances}
        fresh_ids = {item.instance_id for item in self.public_fresh.instances}
        if development_ids & fresh_ids:
            raise ValueError("development and public fresh instance ids must be disjoint")
        asset_ids = [item.asset_id for item in self.assets]
        asset_paths = [item.path for item in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("benchmark asset ids must be unique")
        if len(asset_paths) != len(set(asset_paths)):
            raise ValueError("benchmark asset paths must be unique")
        return self


class BenchmarkTrialRequest(StrictModel):
    suite_id: str
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm: BenchmarkArm
    trial_seed: int
    proposal_call_limit: int
    candidate_evaluation_limit: int
    token_limit: int
    development_instance_ids: list[str]
    public_fresh_in_trial_payload: Literal[False] = False
    blind_confirmation_in_trial_payload: Literal[False] = False


@dataclass(frozen=True)
class BenchmarkTrialContext:
    suite_id: str
    protocol_sha256: str
    arm: BenchmarkArm
    trial_seed: int
    repo_root: Path
    trial_dir: Path
    budget: BenchmarkBudget
    development_instances: tuple[GraphInstanceSpec, ...]


class ArmTrialSubmission(StrictModel):
    executor_id: str = Field(min_length=1)
    candidate_paths: list[str] = Field(min_length=1)
    proposal_calls_used: int = Field(ge=0)
    token_count_used: int = Field(ge=0)
    metadata: dict[str, str] = Field(default_factory=dict)


class SplitEvaluation(StrictModel):
    split: DatasetVisibility
    instance_count: int = Field(ge=1)
    valid_rate: float = Field(ge=0.0, le=1.0)
    reproducibility_rate: float = Field(ge=0.0, le=1.0)
    mean_baseline_colors: float = Field(gt=0.0)
    mean_candidate_colors: float | None = Field(default=None, gt=0.0)
    mean_relative_improvement: float
    positive_relative_improvement: float = Field(ge=0.0)
    elapsed_seconds: float = Field(ge=0.0)
    failure_reasons: list[str] = Field(default_factory=list)


class CandidateBenchmarkEvaluation(StrictModel):
    candidate_id: str
    candidate_path: str
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    development: SplitEvaluation
    public_fresh: SplitEvaluation


class BenchmarkTrialReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: str
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm: BenchmarkArm
    trial_seed: int
    executor_id: str
    proposal_calls_used: int = Field(ge=0)
    candidate_evaluations_used: int = Field(ge=1)
    token_count_used: int = Field(ge=0)
    wall_seconds_used: float = Field(ge=0.0)
    environment: EnvironmentReceipt
    selected_candidate_id: str
    selected_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_development: SplitEvaluation
    selected_public_fresh: SplitEvaluation
    candidate_evaluations: list[CandidateBenchmarkEvaluation] = Field(min_length=1)
    authority: Literal["MECHANICS_AND_PUBLIC_BENCHMARK_ONLY"] = (
        "MECHANICS_AND_PUBLIC_BENCHMARK_ONLY"
    )
    blind: Literal[False] = False
    held_out: Literal[False] = False
    superiority_claim_permitted: Literal[False] = False


class BenchmarkTrialEnvelope(StrictModel):
    receipt: BenchmarkTrialReceipt
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BenchmarkArmSummary(StrictModel):
    arm: BenchmarkArm
    trial_count: int = Field(ge=1)
    valid_public_fresh_improvement_per_cost: float = Field(ge=0.0)
    mean_public_fresh_improvement: float
    mean_development_improvement: float
    public_fresh_valid_rate: float = Field(ge=0.0, le=1.0)
    reproducibility_rate: float = Field(ge=0.0, le=1.0)
    invalid_candidate_rate: float = Field(ge=0.0, le=1.0)
    redundant_candidate_rate: float = Field(ge=0.0, le=1.0)
    proposal_calls_used: int = Field(ge=0)
    candidate_evaluations_used: int = Field(ge=1)
    token_count_used: int = Field(ge=0)
    wall_seconds_used: float = Field(ge=0.0)


class BenchmarkSuiteResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: str
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arms: list[BenchmarkArmSummary]
    paired_primary_deltas_vs_vanilla: dict[str, float]
    decision: Literal["NOT_EVALUABLE_BLIND_CONFIRMATION_UNAVAILABLE"] = (
        "NOT_EVALUABLE_BLIND_CONFIRMATION_UNAVAILABLE"
    )
    authority: Literal["META_BENCHMARK_SMOKE_ONLY"] = "META_BENCHMARK_SMOKE_ONLY"
    blind: Literal[False] = False
    held_out: Literal[False] = False
    superiority_claim_permitted: Literal[False] = False
    reasons: list[str] = Field(min_length=1)
