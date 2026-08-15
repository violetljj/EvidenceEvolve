from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from evidence_evolve.models import EnvironmentReceipt, StrictModel
from evidence_evolve.proposals.models import ProposalMaterializerMode


class SearchEngineMode(StrEnum):
    SHINKA_NATIVE = "SHINKA_NATIVE"
    SHINKA_EVIDENCE = "SHINKA_EVIDENCE"
    OPENEVOLVE_NATIVE = "OPENEVOLVE_NATIVE"
    EVIDENCE_NATIVE_EXPERIMENTAL = "EVIDENCE_NATIVE_EXPERIMENTAL"


class SearchRunRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    mode: Literal[SearchEngineMode.SHINKA_NATIVE] = SearchEngineMode.SHINKA_NATIVE
    task_dir: Path
    results_dir: Path
    num_generations: int = Field(gt=0)
    config_fname: str | None = None
    set_overrides: list[str] = Field(default_factory=list)
    max_evaluation_jobs: int | None = Field(default=None, gt=0)
    max_proposal_jobs: int | None = Field(default=None, gt=0)
    max_db_workers: int | None = Field(default=None, gt=0)
    verbose: bool | None = None
    debug: bool = False
    proposal_materializer: ProposalMaterializerMode = (
        ProposalMaterializerMode.UPSTREAM_STRICT
    )

    @field_validator("task_dir", "results_dir")
    @classmethod
    def absolute_runtime_path(cls, value: Path) -> Path:
        return value.resolve()


class ShinkaImportSummary(StrictModel):
    database_path: str
    candidate_count: int = Field(ge=1)
    correct_candidate_count: int = Field(ge=0)
    archive_count: int = Field(ge=0)
    max_generation: int = Field(ge=0)
    best_program_id: str
    best_combined_score: float
    total_api_cost: float = Field(ge=0.0)
    input_tokens_observed: int = Field(ge=0)
    output_tokens_observed: int = Field(ge=0)
    token_accounting: Literal["UPSTREAM_PROGRAM_METADATA_BEST_EFFORT"] = (
        "UPSTREAM_PROGRAM_METADATA_BEST_EFFORT"
    )
    invalid_candidate_rate: float = Field(ge=0.0, le=1.0)
    attempt_event_count: int = Field(ge=0)
    generation_event_count: int = Field(ge=0)
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SearchRunReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    mode: Literal[SearchEngineMode.SHINKA_NATIVE] = SearchEngineMode.SHINKA_NATIVE
    upstream_distribution: Literal["shinka-evolve"] = "shinka-evolve"
    upstream_version: Literal["0.0.7"] = "0.0.7"
    upstream_repository: Literal["https://github.com/SakanaAI/ShinkaEvolve"] = (
        "https://github.com/SakanaAI/ShinkaEvolve"
    )
    upstream_source_commit: Literal[
        "c4568adde253cacf185be3a8412c3c2142761ebe"
    ] = "c4568adde253cacf185be3a8412c3c2142761ebe"
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_config_fields: dict[str, list[str]]
    task_asset_hashes: dict[str, str]
    upstream_artifact_hashes: dict[str, str]
    results_dir: str
    environment: EnvironmentReceipt
    imported: ShinkaImportSummary
    search_score_authority: Literal["SHINKA_SCHEDULING_ONLY"] = (
        "SHINKA_SCHEDULING_ONLY"
    )
    scientific_outcome_authority: Literal["NONE"] = "NONE"
    proposal_materializer: ProposalMaterializerMode = (
        ProposalMaterializerMode.UPSTREAM_STRICT
    )
    claim_scope: Literal[
        "UPSTREAM_NATIVE_EXECUTION_AND_IMPORT_ONLY",
        "UPSTREAM_SEARCH_WITH_EVIDENCE_EVOLVE_MATERIALIZATION_AND_IMPORT",
    ] = (
        "UPSTREAM_NATIVE_EXECUTION_AND_IMPORT_ONLY"
    )
    superiority_claim_permitted: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)
