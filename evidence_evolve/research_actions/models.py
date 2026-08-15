from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.models import StrictModel


class ActionState(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    WAITING_FOR_AUTHORITY = "WAITING_FOR_AUTHORITY"


class ActionOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_WITH_GAPS = "SUCCEEDED_WITH_GAPS"
    FAILED = "FAILED"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class SourceKind(StrEnum):
    PAPER = "PAPER"
    REPOSITORY = "REPOSITORY"


class SourceArtifact(StrictModel):
    artifact_id: str
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    source_url: str


class IntelligenceRecord(StrictModel):
    source_id: str
    kind: SourceKind
    canonical_id: str
    title: str
    url: str
    summary: str
    authors: list[str] = Field(default_factory=list)
    published_at: str | None = None
    license: str | None = None
    open_access: bool | None = None
    repository_commit: str | None = None
    inspected_paths: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    applicability: dict[str, str] = Field(default_factory=dict)
    authority: Literal["INSPIRATION_ONLY"] = "INSPIRATION_ONLY"


class ResearchActionJob(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    action_id: str
    campaign_id: str
    generation_id: str | None = None
    action: ResearchAction
    query: str
    max_papers: int = Field(default=5, ge=0, le=25)
    max_repositories: int = Field(default=2, ge=0, le=10)
    max_source_files_per_repository: int = Field(default=3, ge=0, le=20)
    authority: Literal["SCHEDULING_ONLY"] = "SCHEDULING_ONLY"

    @model_validator(mode="after")
    def action_has_work(self) -> "ResearchActionJob":
        if self.action is ResearchAction.SEARCH_LITERATURE:
            if self.max_papers + self.max_repositories <= 0:
                raise ValueError("literature intelligence action has no requested sources")
        return self


class ActionExecutionResult(StrictModel):
    outcome: ActionOutcome
    records: list[IntelligenceRecord] = Field(default_factory=list)
    artifacts: list[SourceArtifact] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class ResearchActionReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    action_receipt_id: str
    job: ResearchActionJob
    started_at_utc: str
    completed_at_utc: str
    outcome: ActionOutcome
    records: list[IntelligenceRecord] = Field(default_factory=list)
    artifacts: list[SourceArtifact] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    authority: Literal["SCHEDULING_ONLY"] = "SCHEDULING_ONLY"


class ActionReceiptEnvelope(StrictModel):
    receipt: ResearchActionReceipt
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActionRunResult(StrictModel):
    state: ActionState
    receipt: ActionReceiptEnvelope | None = None
    reason: str | None = None
