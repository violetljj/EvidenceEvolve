from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from evidence_evolve.models import EnvironmentReceipt, StrictModel


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ProposalDialect(StrEnum):
    SEARCH_REPLACE = "SEARCH_REPLACE"


class MatchMode(StrEnum):
    EXACT_UNIQUE = "EXACT_UNIQUE"
    IGNORE_BLANK_LINES_UNIQUE = "IGNORE_BLANK_LINES_UNIQUE"


class ProposalMaterializerMode(StrEnum):
    UPSTREAM_STRICT = "UPSTREAM_STRICT"
    EVIDENCE_EVOLVE_V1 = "EVIDENCE_EVOLVE_V1"


class SearchReplaceEdit(StrictModel):
    search: str
    replace: str

    @model_validator(mode="after")
    def search_is_not_empty(self) -> "SearchReplaceEdit":
        if not self.search.strip():
            raise ValueError("empty SEARCH blocks are not admitted")
        return self


class ProposalIR(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    proposal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    source_system: str = Field(min_length=1)
    dialect: Literal[ProposalDialect.SEARCH_REPLACE] = (
        ProposalDialect.SEARCH_REPLACE
    )
    language: Literal["python"] = "python"
    target_sha256: Sha256
    raw_response_sha256: Sha256
    extracted_patch_sha256: Sha256
    name: str | None = None
    description: str | None = None
    edits: Annotated[list[SearchReplaceEdit], Field(min_length=1)]
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppliedEdit(StrictModel):
    edit_index: int = Field(ge=0)
    match_mode: MatchMode
    target_start: int = Field(ge=0)
    target_end: int = Field(ge=0)


class MaterializationReceipt(StrictModel):
    proposal_id: str
    target_sha256: Sha256
    candidate_sha256: Sha256
    applied_edits: Annotated[list[AppliedEdit], Field(min_length=1)]
    immutable_regions_preserved: Literal[True] = True
    markers_preserved: Literal[True] = True


class MechanicsAdmissionThresholds(StrictModel):
    patch_apply_success_rate_min: float = Field(ge=0.0, le=1.0)
    candidate_compile_success_rate_min: float = Field(ge=0.0, le=1.0)
    evaluator_reached_rate_min: float = Field(ge=0.0, le=1.0)
    nonbaseline_candidate_score_required_per_arm: int = Field(ge=1)


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if not normalized or normalized.startswith("/") or ".." in parts:
        raise ValueError("artifact paths must be repository-relative")
    return normalized


class MechanicsAdmissionProtocol(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    source_campaign: str = Field(min_length=1)
    corpus_path: str
    corpus_sha256: Sha256
    target_program_path: str
    target_program_sha256: Sha256
    evaluator_path: str
    evaluator_sha256: Sha256
    language: Literal["python"] = "python"
    baseline_score: float
    case_ids: Annotated[list[str], Field(min_length=1)]
    thresholds: MechanicsAdmissionThresholds
    evaluator_timeout_seconds: int = Field(gt=0, le=3600)
    remote_model_calls_permitted: Literal[False] = False

    _normalize_corpus = field_validator("corpus_path")(_relative_path)
    _normalize_target = field_validator("target_program_path")(_relative_path)
    _normalize_evaluator = field_validator("evaluator_path")(_relative_path)

    @field_validator("case_ids")
    @classmethod
    def unique_case_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("case_ids must be unique")
        return values


class MechanicsCaseReceipt(StrictModel):
    case_id: str
    arm: str
    proposal_extracted: bool
    patch_applied: bool
    candidate_compiled: bool
    evaluator_invoked: bool
    evaluator_reached: bool
    evaluator_valid: bool | None = None
    candidate_score: float | None = None
    nonbaseline_score: bool = False
    target_sha256: Sha256
    candidate_sha256: Sha256 | None = None
    match_modes: list[MatchMode] = Field(default_factory=list)
    failure_stage: str | None = None
    failure_reason: str | None = None
    candidate_path: str | None = None
    evaluator_results_dir: str | None = None


class CandidateSurvivalFunnel(StrictModel):
    proposals: int = Field(ge=1)
    proposal_extracted: int = Field(ge=0)
    patchable: int = Field(ge=0)
    runnable: int = Field(ge=0)
    evaluator_reached: int = Field(ge=0)
    evaluator_valid: int = Field(ge=0)
    nonbaseline_score: int = Field(ge=0)
    patch_apply_success_rate: float = Field(ge=0.0, le=1.0)
    candidate_compile_success_rate: float = Field(ge=0.0, le=1.0)
    evaluator_reached_rate: float = Field(ge=0.0, le=1.0)


class MechanicsAdmissionReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: str
    protocol_sha256: Sha256
    source_campaign: str
    mechanics_status: Literal["PASS", "FAIL"]
    admitted_for_expensive_search: bool
    failure_outcome: Literal["INVALID_MECHANICS_OR_ADAPTER"] | None = None
    remote_model_calls: Literal[0] = 0
    elapsed_seconds: float = Field(ge=0.0)
    implementation_hashes: dict[str, Sha256]
    environment: EnvironmentReceipt
    thresholds: MechanicsAdmissionThresholds
    funnel: CandidateSurvivalFunnel
    per_arm_funnel: dict[str, CandidateSurvivalFunnel]
    threshold_checks: dict[str, bool]
    cases: list[MechanicsCaseReceipt]
    scientific_outcome_authority: Literal["NONE"] = "NONE"
    observation: str

    @model_validator(mode="after")
    def failure_class_matches_status(self) -> "MechanicsAdmissionReceipt":
        if self.admitted_for_expensive_search != (self.mechanics_status == "PASS"):
            raise ValueError("admission must match mechanics status")
        if self.mechanics_status == "FAIL" and self.failure_outcome is None:
            raise ValueError("failed mechanics must be classified")
        if self.mechanics_status == "PASS" and self.failure_outcome is not None:
            raise ValueError("passed mechanics cannot carry a failure outcome")
        return self
