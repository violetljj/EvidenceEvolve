from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EvidenceGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class EvidencePermission(StrEnum):
    TRAIN = "TRAIN"
    DEV = "DEV"
    CONFIRM = "CONFIRM"
    CLAIM = "CLAIM"
    CANDIDATE_MINING = "CANDIDATE_MINING"


class ScientificOutcome(StrEnum):
    POSITIVE_HEADROOM = "POSITIVE_HEADROOM"
    VALID_NEGATIVE = "VALID_NEGATIVE"
    NOT_EVALUABLE_DATA = "NOT_EVALUABLE_DATA"
    INVALID_MECHANICS_OR_ADAPTER = "INVALID_MECHANICS_OR_ADAPTER"


class GateDecision(StrEnum):
    ADMIT = "ADMIT"
    MUTATE = "MUTATE"
    KILL = "KILL"
    PAUSE_NOT_EVALUABLE = "PAUSE_NOT_EVALUABLE"
    REPAIR_IMPLEMENTATION = "REPAIR_IMPLEMENTATION"
    INVALID_PROTOCOL_TAMPERING = "INVALID_PROTOCOL_TAMPERING"


class ArchiveClass(StrEnum):
    ELITE = "ELITE"
    STEPPING_STONE = "STEPPING_STONE"
    VALID_NEGATIVE = "VALID_NEGATIVE"
    PAUSED_NOT_EVALUABLE = "PAUSED_NOT_EVALUABLE"
    INVALID = "INVALID"


class ResearchStage(StrEnum):
    P0_PROTOCOL_LOCK = "P0_PROTOCOL_LOCK"
    M0_MECHANICS = "M0_MECHANICS"
    H0_REAL_HEADROOM = "H0_REAL_HEADROOM"
    T0_LEARNED_CANDIDATE = "T0_LEARNED_CANDIDATE"
    C0_CONFIRMATION = "C0_CONFIRMATION"
    D0_DEPLOYMENT = "D0_DEPLOYMENT"


class MechanicsStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASS = "PASS"
    FAIL = "FAIL"


class MutationType(StrEnum):
    MECHANISM = "mechanism_mutation"
    REPRESENTATION = "representation_mutation"
    FAILURE_DIRECTED = "failure_directed_mutation"
    CONTROL = "control_mutation"
    SIMPLIFICATION = "simplification"
    CROSS_FAMILY = "cross_family"
    RESTART = "restart"


class ObjectiveDirection(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class FrozenAssetKind(StrEnum):
    EVALUATOR = "evaluator"
    HARNESS_CORE = "harness_core"
    DATA_MANIFEST = "data_manifest"
    PROTOCOL = "protocol"
    CONFIRMATION = "confirmation"
    MODEL = "model"


def _validate_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be repository-relative and cannot contain '..'")
    if not normalized or normalized == ".":
        raise ValueError("path cannot be empty")
    return normalized


class Campaign(StrictModel):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")]
    base_commit: str
    research_question: Annotated[str, Field(min_length=12)]
    claim_scope: Annotated[str, Field(min_length=3)]


class Authority(StrictModel):
    protocol_mutable: Literal[False] = False
    evaluator_mutable: Literal[False] = False
    confirmation_visible_to_agents: Literal[False] = False
    final_merge_requires_human: bool = True


class EditableScope(StrictModel):
    allow: Annotated[list[str], Field(min_length=1)]
    deny: Annotated[list[str], Field(min_length=1)]

    @field_validator("allow", "deny")
    @classmethod
    def normalize_patterns(cls, values: list[str]) -> list[str]:
        return [_validate_relative_path(value) for value in values]

    @model_validator(mode="after")
    def exact_patterns_do_not_overlap(self) -> "EditableScope":
        overlap = set(self.allow) & set(self.deny)
        if overlap:
            raise ValueError(f"allow and deny contain identical patterns: {sorted(overlap)}")
        return self


class EvidenceSource(StrictModel):
    source_id: str
    grade: EvidenceGrade
    path: str
    permissions: set[EvidencePermission]
    sha256: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None

    _normalize_path = field_validator("path")(_validate_relative_path)


class FrozenAsset(StrictModel):
    asset_id: str
    kind: FrozenAssetKind
    path: str
    sha256: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None

    _normalize_path = field_validator("path")(_validate_relative_path)


class MetricConstraint(StrictModel):
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def has_a_bound(self) -> "MetricConstraint":
        if self.min is None and self.max is None:
            raise ValueError("constraint must define min, max, or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("constraint min cannot exceed max")
        return self


class MetricsPolicy(StrictModel):
    hard_constraints: Annotated[dict[str, MetricConstraint], Field(min_length=1)]
    pareto_objectives: Annotated[dict[str, ObjectiveDirection], Field(min_length=1)]


class Budgets(StrictModel):
    proposal_calls: Annotated[int, Field(ge=0)] = 0
    implementations: Annotated[int, Field(ge=0)] = 0
    mechanics_runs: Annotated[int, Field(ge=0)] = 0
    proxy_runs: Annotated[int, Field(ge=0)] = 0
    confirmation_runs: Annotated[int, Field(ge=0)] = 0
    device_runs: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def has_finite_work(self) -> "Budgets":
        if not any(self.model_dump().values()):
            raise ValueError("at least one budget must be greater than zero")
        return self


class ContractLock(StrictModel):
    algorithm: Literal["sha256"] = "sha256"
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ResearchContract(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    campaign: Campaign
    authority: Authority = Field(default_factory=Authority)
    editable_scope: EditableScope
    evidence_sources: Annotated[list[EvidenceSource], Field(min_length=1)]
    frozen_assets: Annotated[list[FrozenAsset], Field(min_length=1)]
    metrics: MetricsPolicy
    budgets: Budgets
    closure_registry: str
    lock: ContractLock | None = None

    _normalize_registry_path = field_validator("closure_registry")(_validate_relative_path)


class ExpectedSignature(StrictModel):
    improve: Annotated[list[str], Field(min_length=1)]
    unchanged: list[str] = Field(default_factory=list)


class CandidateGenome(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")]
    parent_ids: Annotated[list[str], Field(min_length=1)]
    island: str
    family: str
    mutation_type: MutationType
    hypothesis: Annotated[str, Field(min_length=12)]
    intervention: Annotated[str, Field(min_length=8)]
    expected_signature: ExpectedSignature
    falsifier: Annotated[str, Field(min_length=8)]
    required_controls: Annotated[list[str], Field(min_length=1)]
    editable_files: Annotated[list[str], Field(min_length=1)]
    estimated_cost_tier: Annotated[int, Field(ge=0, le=5)]
    reopen_condition_claims: list[str] = Field(default_factory=list)

    @field_validator("editable_files")
    @classmethod
    def normalize_editable_files(cls, values: list[str]) -> list[str]:
        return [_validate_relative_path(value) for value in values]


class EvaluationInput(StrictModel):
    contract_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    candidate: CandidateGenome
    stage: ResearchStage
    changed_files: list[str] = Field(default_factory=list)
    protocol_violations: list[str] = Field(default_factory=list)
    mechanics_status: MechanicsStatus = MechanicsStatus.NOT_RUN
    data_eligible: bool = False
    data_ineligibility_reasons: list[str] = Field(default_factory=list)
    data_leakage: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)
    controls: dict[str, bool] = Field(default_factory=dict)
    scientific_outcome: ScientificOutcome | None = None
    verified_reopen_conditions: set[str] = Field(default_factory=set)


class ConstraintCheck(StrictModel):
    metric: str
    value: float | None
    min: float | None = None
    max: float | None = None
    passed: bool
    reason: str


class GateVerdict(StrictModel):
    decision: GateDecision
    archive_class: ArchiveClass
    scientific_outcome: ScientificOutcome
    reasons: Annotated[list[str], Field(min_length=1)]
    constraint_checks: dict[str, ConstraintCheck] = Field(default_factory=dict)
    controls_complete: bool
    protocol_valid: bool


class EnvironmentReceipt(StrictModel):
    python: str
    platform: str
    executable: str
    extra: dict[str, str] = Field(default_factory=dict)


class EvaluationReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    receipt_id: str
    created_at_utc: str
    campaign_id: str
    candidate_id: str
    base_commit: str
    candidate_commit: str | None = None
    patch_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    evaluator_hashes: dict[str, str]
    data_hashes: dict[str, str]
    seed: int
    command: list[str]
    elapsed_seconds: Annotated[float, Field(ge=0)]
    environment: EnvironmentReceipt
    evaluation_input: EvaluationInput
    verdict: GateVerdict


class ReceiptEnvelope(StrictModel):
    receipt: EvaluationReceipt
    receipt_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
