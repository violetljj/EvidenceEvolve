from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from evidence_evolve.artifacts import load_receipt
from evidence_evolve.hashing import sha256_object
from evidence_evolve.models import (
    ClaimCeiling,
    MechanicsStatus,
    ResearchStage,
    ScientificOutcome,
    StrictModel,
)


class MemoryKind(StrEnum):
    RESULT = "RESULT"
    MECHANISM = "MECHANISM"
    FAILURE = "FAILURE"
    PROCEDURE = "PROCEDURE"
    FRONTIER = "FRONTIER"
    LINEAGE = "LINEAGE"
    TRANSFER = "TRANSFER"


class MemoryStatus(StrEnum):
    ACTIVE_SCHEDULING_MEMORY = "ACTIVE_SCHEDULING_MEMORY"
    EVIDENCE_BOUND = "EVIDENCE_BOUND"
    QUARANTINED = "QUARANTINED"
    SUPERSEDED = "SUPERSEDED"
    RETIRED = "RETIRED"


class MemoryRole(StrEnum):
    RESEARCH_DIRECTOR = "RESEARCH_DIRECTOR"
    HYPOTHESIS_EXPLORER = "HYPOTHESIS_EXPLORER"
    IMPLEMENTER = "IMPLEMENTER"
    SCIENTIST = "SCIENTIST"
    RED_QUEEN = "RED_QUEEN"
    GATE_ENGINE = "GATE_ENGINE"


ROLE_MEMORY_KINDS: dict[MemoryRole, frozenset[MemoryKind]] = {
    MemoryRole.RESEARCH_DIRECTOR: frozenset(MemoryKind),
    MemoryRole.HYPOTHESIS_EXPLORER: frozenset(
        {
            MemoryKind.MECHANISM,
            MemoryKind.RESULT,
            MemoryKind.FAILURE,
            MemoryKind.FRONTIER,
            MemoryKind.LINEAGE,
            MemoryKind.TRANSFER,
        }
    ),
    MemoryRole.IMPLEMENTER: frozenset(
        {MemoryKind.PROCEDURE, MemoryKind.FAILURE, MemoryKind.LINEAGE}
    ),
    MemoryRole.SCIENTIST: frozenset(
        {
            MemoryKind.MECHANISM,
            MemoryKind.RESULT,
            MemoryKind.FAILURE,
            MemoryKind.PROCEDURE,
            MemoryKind.FRONTIER,
            MemoryKind.LINEAGE,
            MemoryKind.TRANSFER,
        }
    ),
    MemoryRole.RED_QUEEN: frozenset(
        {MemoryKind.FAILURE, MemoryKind.FRONTIER}
    ),
    # The frozen gate consumes only frozen evidence, never derived memory.
    MemoryRole.GATE_ENGINE: frozenset(),
}


class MemoryScope(StrictModel):
    project: str = "EvidenceEvolve"
    campaign: str
    family: str
    stage: ResearchStage | Literal["RESEARCH_INTELLIGENCE"]
    visibility: Literal["DEVELOPMENT"] = "DEVELOPMENT"


class MemoryProvenance(StrictModel):
    receipt_ids: list[str] = Field(default_factory=list)
    receipt_sha256: dict[str, str] = Field(default_factory=dict)
    action_receipt_ids: list[str] = Field(default_factory=list)
    action_receipt_sha256: dict[str, str] = Field(default_factory=dict)
    source_artifact_sha256: dict[str, str] = Field(default_factory=dict)
    candidate_commit: str | None = None
    patch_sha256: str | None = None
    evaluator_hashes: dict[str, str] = Field(default_factory=dict)
    data_hashes: dict[str, str] = Field(default_factory=dict)
    compiler_version: Literal["scientific-memory-v2"] = "scientific-memory-v2"

    @model_validator(mode="after")
    def has_immutable_source_binding(self) -> "MemoryProvenance":
        if not self.receipt_ids and not self.action_receipt_ids:
            raise ValueError("memory card must bind an evidence or action receipt")
        if set(self.receipt_ids) != set(self.receipt_sha256):
            raise ValueError("receipt ids and hashes must match")
        if set(self.action_receipt_ids) != set(self.action_receipt_sha256):
            raise ValueError("action receipt ids and hashes must match")
        return self


class MemoryEpistemics(StrictModel):
    authority: Literal["SCHEDULING_ONLY"] = "SCHEDULING_ONLY"
    claim_ceiling: ClaimCeiling = ClaimCeiling.DEVELOPMENT_ONLY
    scientific_outcome: ScientificOutcome | None = None
    mechanism_support: str | None = None
    evidence_basis: Literal["INTERNAL_RECEIPT", "EXTERNAL_SOURCE"] = (
        "INTERNAL_RECEIPT"
    )
    source_binding_verified: Literal[True] = True


class MemoryContent(StrictModel):
    hypothesis: str
    intervention: str
    mechanism_claims: list[str] = Field(default_factory=list)
    expected_signatures: dict[str, list[str]] = Field(default_factory=dict)
    observed_signatures: dict[str, dict[str, object]] = Field(default_factory=dict)
    observed_metrics: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    failure_signature: list[str] = Field(default_factory=list)
    applicability: dict[str, str] = Field(default_factory=dict)
    non_applicability: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    procedure: list[str] = Field(default_factory=list)
    lineage: dict[str, object] = Field(default_factory=dict)
    source_handles: list[str] = Field(default_factory=list)


class ScientificMemoryCard(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    memory_id: str
    version: int = Field(default=1, ge=1)
    kind: MemoryKind
    status: MemoryStatus
    scope: MemoryScope
    provenance: MemoryProvenance
    epistemics: MemoryEpistemics
    content: MemoryContent
    created_at_utc: str


class RoleScopedMemoryPacket(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    retrieval_event_id: str
    role: MemoryRole
    authority: Literal["SCHEDULING_ONLY"] = "SCHEDULING_ONLY"
    query: str | None = None
    cards: list[ScientificMemoryCard] = Field(default_factory=list)


class MemoryIntegrityError(RuntimeError):
    pass


def _claim_ceiling(outcome: ScientificOutcome, stage: ResearchStage) -> ClaimCeiling:
    if outcome is not ScientificOutcome.POSITIVE_HEADROOM:
        return ClaimCeiling.DEVELOPMENT_ONLY
    return {
        ResearchStage.H0_REAL_HEADROOM: ClaimCeiling.TRAINING_ELIGIBLE,
        ResearchStage.T0_LEARNED_CANDIDATE: ClaimCeiling.CONFIRMATION_ELIGIBLE,
        ResearchStage.C0_CONFIRMATION: ClaimCeiling.CLAIM_ELIGIBLE_FOR_HUMAN,
        ResearchStage.D0_DEPLOYMENT: ClaimCeiling.DEPLOYMENT_ELIGIBLE_FOR_HUMAN,
    }.get(stage, ClaimCeiling.DEVELOPMENT_ONLY)


def _memory_id(receipt_id: str, kind: MemoryKind) -> str:
    digest = hashlib.sha256(f"{receipt_id}\0{kind.value}".encode("utf-8")).hexdigest()
    return f"MEM-{digest[:24]}"


def _fts_query(query: str) -> str | None:
    tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if not tokens:
        return None
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


class ResearchMemoryStore:
    """Evidence-bound, scheduling-only projection over immutable receipts.

    Cards and search indexes are rebuildable projections. Receipt envelopes remain
    the only evidence authority; this store never changes a gate verdict.
    """

    def __init__(self, database: Path):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_cards (
                    memory_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    campaign TEXT NOT NULL,
                    family TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    authority TEXT NOT NULL CHECK(authority = 'SCHEDULING_ONLY'),
                    claim_ceiling TEXT NOT NULL,
                    scientific_outcome TEXT NOT NULL,
                    mechanism_support TEXT,
                    card_json TEXT NOT NULL,
                    searchable_text TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    PRIMARY KEY(memory_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_cards_filter
                    ON memory_cards(status, visibility, campaign, family, stage, kind);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    memory_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    receipt_id TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    PRIMARY KEY(memory_id, version, receipt_id),
                    FOREIGN KEY(memory_id, version)
                        REFERENCES memory_cards(memory_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_sources_receipt
                    ON memory_sources(receipt_id);
                CREATE TABLE IF NOT EXISTS memory_action_sources (
                    memory_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    action_receipt_id TEXT NOT NULL,
                    action_receipt_sha256 TEXT NOT NULL,
                    source_artifact_sha256_json TEXT NOT NULL,
                    PRIMARY KEY(memory_id, version, action_receipt_id),
                    FOREIGN KEY(memory_id, version)
                        REFERENCES memory_cards(memory_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_action_sources_receipt
                    ON memory_action_sources(action_receipt_id);
                CREATE TABLE IF NOT EXISTS memory_retrieval_events (
                    retrieval_event_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    query TEXT,
                    filters_json TEXT NOT NULL,
                    returned_card_keys_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_cards_fts USING fts5(
                    card_key UNINDEXED,
                    searchable_text,
                    tokenize = 'unicode61'
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def compile_history(self) -> list[ScientificMemoryCard]:
        with self._connect() as connection:
            receipt_ids = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT er.receipt_id
                    FROM evaluation_receipts AS er
                    INNER JOIN mechanism_assessments AS ma
                      ON ma.receipt_id = er.receipt_id
                    ORDER BY er.created_at_utc, er.receipt_id
                    """
                ).fetchall()
            ]
        cards: list[ScientificMemoryCard] = []
        for receipt_id in receipt_ids:
            cards.extend(self.compile_receipt(receipt_id))
        with self._connect() as connection:
            has_actions = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_action_receipts'"
            ).fetchone()
            action_receipt_ids = (
                [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT action_receipt_id FROM research_action_receipts
                        ORDER BY created_at_utc, action_receipt_id
                        """
                    ).fetchall()
                ]
                if has_actions
                else []
            )
        for action_receipt_id in action_receipt_ids:
            cards.extend(self.compile_action_receipt(action_receipt_id))
        return cards

    def compile_receipt(self, receipt_id: str) -> list[ScientificMemoryCard]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT er.receipt_path, er.receipt_sha256, ma.assessment_json
                FROM evaluation_receipts AS er
                INNER JOIN mechanism_assessments AS ma
                  ON ma.receipt_id = er.receipt_id
                WHERE er.receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
        if row is None:
            return []
        receipt_path, indexed_sha256, assessment_json = row
        envelope = load_receipt(self.database.parent / receipt_path)
        if envelope.receipt_sha256 != indexed_sha256:
            raise MemoryIntegrityError(
                f"receipt index hash mismatch for memory source: {receipt_id}"
            )
        receipt = envelope.receipt
        # Confirmation evidence is deliberately not projected into agent memory.
        if receipt.evaluation_input.stage is ResearchStage.C0_CONFIRMATION:
            return []

        candidate = receipt.evaluation_input.candidate
        assessment = json.loads(assessment_json)
        observed = assessment.get("signature_checks", {})
        failure_signature = sorted(
            set(receipt.verdict.reasons)
            | set(assessment.get("reasons", []))
            | {
                f"SIGNATURE:{name}:{check.get('reason')}"
                for name, check in observed.items()
                if check.get("judgement") == "CONTRADICTED"
            }
        )
        unresolved = sorted(
            set(assessment.get("missing_ablations", []))
            | {
                f"SIGNATURE:{name}:{check.get('reason')}"
                for name, check in observed.items()
                if check.get("judgement") == "NOT_ASSESSED"
            }
        )
        if assessment.get("support") in {"INCONCLUSIVE", "NOT_EVALUABLE"}:
            unresolved.extend(assessment.get("reasons", []))
        unresolved = sorted(set(unresolved))

        base_content = MemoryContent(
            hypothesis=candidate.hypothesis,
            intervention=candidate.intervention,
            mechanism_claims=candidate.mechanism_claims,
            expected_signatures=candidate.expected_signature.model_dump(mode="json"),
            observed_signatures=observed,
            observed_metrics=receipt.evaluation_input.metrics,
            assumptions=candidate.assumptions,
            failure_signature=failure_signature,
            applicability=candidate.behavior_descriptor,
            non_applicability=[
                "No cross-campaign authority without target-specific revalidation",
                "No confirmation, deployment, product, or safety authority from this card",
            ],
            unresolved_questions=unresolved,
            source_handles=[
                f"receipt:{receipt.receipt_id}",
                f"candidate:{candidate.candidate_id}",
                f"patch:{receipt.patch_sha256}" if receipt.patch_sha256 else "patch:none",
            ],
            lineage={
                "candidate_id": candidate.candidate_id,
                "parent_ids": candidate.parent_ids,
                "genetic_parent_id": receipt.genetic_parent_id,
                "genetic_parent_commit": receipt.genetic_parent_commit,
                "candidate_commit": receipt.candidate_commit,
                "mutation_type": candidate.mutation_type.value,
                "patch_sha256": receipt.patch_sha256,
                "parent_patch_sha256": receipt.parent_patch_sha256,
            },
        )
        active = (
            receipt.verdict.protocol_valid
            and receipt.verdict.scientific_outcome
            is not ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
        )
        status = (
            MemoryStatus.ACTIVE_SCHEDULING_MEMORY
            if active
            else MemoryStatus.QUARANTINED
        )
        kinds = [MemoryKind.RESULT, MemoryKind.MECHANISM, MemoryKind.LINEAGE]
        if (
            receipt.verdict.scientific_outcome is ScientificOutcome.VALID_NEGATIVE
            or assessment.get("support") == "CONTRADICTED"
        ):
            kinds.append(MemoryKind.FAILURE)
        if (
            receipt.evaluation_input.mechanics_status is MechanicsStatus.PASS
            and receipt.verdict.protocol_valid
        ):
            kinds.append(MemoryKind.PROCEDURE)
        if (
            receipt.verdict.scientific_outcome is ScientificOutcome.NOT_EVALUABLE_DATA
            or assessment.get("support") in {"INCONCLUSIVE", "NOT_EVALUABLE"}
            or unresolved
        ):
            kinds.append(MemoryKind.FRONTIER)

        cards: list[ScientificMemoryCard] = []
        for kind in dict.fromkeys(kinds):
            content = base_content.model_copy(deep=True)
            if kind is MemoryKind.PROCEDURE:
                content.procedure = [
                    "Dereference and verify the immutable source receipt",
                    f"Reproduce with command: {json.dumps(receipt.command)}",
                    "Verify candidate commit, patch, evaluator, data, and contract hashes",
                ]
            elif kind is MemoryKind.FRONTIER and candidate.falsifier not in content.unresolved_questions:
                content.unresolved_questions.append(candidate.falsifier)
            card = ScientificMemoryCard(
                memory_id=_memory_id(receipt.receipt_id, kind),
                kind=kind,
                status=status,
                scope=MemoryScope(
                    campaign=receipt.campaign_id,
                    family=candidate.family,
                    stage=receipt.evaluation_input.stage,
                ),
                provenance=MemoryProvenance(
                    receipt_ids=[receipt.receipt_id],
                    receipt_sha256={receipt.receipt_id: envelope.receipt_sha256},
                    candidate_commit=receipt.candidate_commit,
                    patch_sha256=receipt.patch_sha256,
                    evaluator_hashes=receipt.evaluator_hashes,
                    data_hashes=receipt.data_hashes,
                ),
                epistemics=MemoryEpistemics(
                    claim_ceiling=_claim_ceiling(
                        receipt.verdict.scientific_outcome,
                        receipt.evaluation_input.stage,
                    ),
                    scientific_outcome=receipt.verdict.scientific_outcome,
                    mechanism_support=assessment.get("support"),
                ),
                content=content,
                created_at_utc=receipt.created_at_utc,
            )
            self._insert_card(card)
            cards.append(card)
        return cards

    def compile_action_receipt(
        self, action_receipt_id: str
    ) -> list[ScientificMemoryCard]:
        from evidence_evolve.research_actions.models import (
            ActionOutcome,
            ActionReceiptEnvelope,
            SourceKind,
        )

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT receipt_path, receipt_sha256
                FROM research_action_receipts WHERE action_receipt_id = ?
                """,
                (action_receipt_id,),
            ).fetchone()
        if row is None:
            return []
        path, indexed_sha256 = row
        envelope = ActionReceiptEnvelope.model_validate_json(
            (self.database.parent / path).read_text(encoding="utf-8")
        )
        actual = sha256_object(envelope.receipt)
        if actual != envelope.receipt_sha256 or actual != indexed_sha256:
            raise MemoryIntegrityError(
                f"action receipt hash mismatch for memory source: {action_receipt_id}"
            )
        receipt = envelope.receipt
        if receipt.outcome not in {
            ActionOutcome.SUCCEEDED,
            ActionOutcome.SUCCEEDED_WITH_GAPS,
        }:
            return []
        artifact_hashes = {
            artifact.artifact_id: artifact.sha256 for artifact in receipt.artifacts
        }
        cards: list[ScientificMemoryCard] = []
        for record in receipt.records:
            kinds = (
                [MemoryKind.MECHANISM]
                if record.kind is SourceKind.PAPER
                else [MemoryKind.PROCEDURE, MemoryKind.TRANSFER]
            )
            for kind in kinds:
                digest = hashlib.sha256(
                    f"{action_receipt_id}\0{record.source_id}\0{kind.value}".encode()
                ).hexdigest()
                is_repository = record.kind is SourceKind.REPOSITORY
                is_procedure = kind is MemoryKind.PROCEDURE
                compact_summary = record.summary[:2400]
                content = MemoryContent(
                    hypothesis=f"External source for investigation: {record.title}",
                    intervention=(
                        "Inspect the pinned repository and reproduce the relevant procedure"
                        if is_procedure
                        else "Assess whether this external mechanism transfers to the target campaign"
                    ),
                    mechanism_claims=(
                        []
                        if is_procedure or not compact_summary
                        else [compact_summary]
                    ),
                    applicability=record.applicability,
                    non_applicability=[
                        "External source is inspiration only, not internal experimental evidence",
                        "Target campaign requires its own implementation, controls, and evaluation",
                    ],
                    procedure=(
                        [
                            f"Inspect repository at immutable commit {record.repository_commit}",
                            *[f"Inspect pinned path: {item}" for item in record.inspected_paths],
                        ]
                        if is_repository
                        else []
                    ),
                    lineage=(
                        {
                            "canonical_id": record.canonical_id,
                            "repository_commit": record.repository_commit,
                            "inspected_paths": record.inspected_paths,
                        }
                        if is_repository
                        else {"canonical_id": record.canonical_id}
                    ),
                    source_handles=[
                        f"action_receipt:{action_receipt_id}",
                        f"external_source:{record.source_id}",
                        record.url,
                        *[f"artifact:{item}" for item in record.artifact_ids],
                    ],
                )
                card = ScientificMemoryCard(
                    memory_id=f"MEM-{digest[:24]}",
                    kind=kind,
                    status=MemoryStatus.ACTIVE_SCHEDULING_MEMORY,
                    scope=MemoryScope(
                        campaign=receipt.job.campaign_id,
                        family=(
                            "external_literature"
                            if record.kind is SourceKind.PAPER
                            else "external_repository"
                        ),
                        stage="RESEARCH_INTELLIGENCE",
                    ),
                    provenance=MemoryProvenance(
                        action_receipt_ids=[action_receipt_id],
                        action_receipt_sha256={
                            action_receipt_id: envelope.receipt_sha256
                        },
                        source_artifact_sha256={
                            artifact_id: artifact_hashes[artifact_id]
                            for artifact_id in record.artifact_ids
                            if artifact_id in artifact_hashes
                        },
                    ),
                    epistemics=MemoryEpistemics(
                        scientific_outcome=None,
                        evidence_basis="EXTERNAL_SOURCE",
                    ),
                    content=content,
                    created_at_utc=receipt.completed_at_utc,
                )
                self._insert_card(card)
                cards.append(card)
        return cards

    def _insert_card(self, card: ScientificMemoryCard) -> None:
        card_json = json.dumps(card.model_dump(mode="json"), sort_keys=True)
        searchable = " ".join(
            [
                card.kind.value,
                card.scope.campaign,
                card.scope.family,
                card.content.hypothesis,
                card.content.intervention,
                *card.content.mechanism_claims,
                *card.content.assumptions,
                *card.content.failure_signature,
                *card.content.unresolved_questions,
                *card.content.applicability.values(),
            ]
        )
        key = f"{card.memory_id}:{card.version}"
        stage_value = (
            card.scope.stage.value
            if isinstance(card.scope.stage, ResearchStage)
            else card.scope.stage
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT card_json FROM memory_cards WHERE memory_id = ? AND version = ?",
                (card.memory_id, card.version),
            ).fetchone()
            if existing is not None:
                if existing[0] != card_json:
                    raise MemoryIntegrityError(
                        f"immutable memory card content changed: {key}"
                    )
                return
            connection.execute(
                """
                INSERT INTO memory_cards(
                    memory_id, version, kind, status, campaign, family, stage,
                    visibility, authority, claim_ceiling, scientific_outcome,
                    mechanism_support, card_json, searchable_text, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card.memory_id,
                    card.version,
                    card.kind.value,
                    card.status.value,
                    card.scope.campaign,
                    card.scope.family,
                    stage_value,
                    card.scope.visibility,
                    card.epistemics.authority,
                    card.epistemics.claim_ceiling.value,
                    (
                        card.epistemics.scientific_outcome.value
                        if card.epistemics.scientific_outcome is not None
                        else "EXTERNAL_SOURCE_ONLY"
                    ),
                    card.epistemics.mechanism_support,
                    card_json,
                    searchable,
                    card.created_at_utc,
                ),
            )
            for source_id, source_sha256 in card.provenance.receipt_sha256.items():
                connection.execute(
                    """
                    INSERT INTO memory_sources(
                        memory_id, version, receipt_id, receipt_sha256
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (card.memory_id, card.version, source_id, source_sha256),
                )
            for action_receipt_id, action_sha256 in (
                card.provenance.action_receipt_sha256.items()
            ):
                connection.execute(
                    """
                    INSERT INTO memory_action_sources(
                        memory_id, version, action_receipt_id,
                        action_receipt_sha256, source_artifact_sha256_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        card.memory_id,
                        card.version,
                        action_receipt_id,
                        action_sha256,
                        json.dumps(
                            card.provenance.source_artifact_sha256, sort_keys=True
                        ),
                    ),
                )
            connection.execute(
                "INSERT INTO memory_cards_fts(card_key, searchable_text) VALUES (?, ?)",
                (key, searchable),
            )

    def retrieve_packet(
        self,
        *,
        role: MemoryRole,
        query: str | None = None,
        campaign: str | None = None,
        family: str | None = None,
        kinds: set[MemoryKind] | None = None,
        limit: int = 8,
    ) -> RoleScopedMemoryPacket:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        allowed = ROLE_MEMORY_KINDS[role]
        requested = allowed if kinds is None else allowed & kinds
        cards: list[ScientificMemoryCard] = []
        fallback_used = False
        if limit and requested:
            cards = self._query_cards(
                requested=requested,
                query=query,
                campaign=campaign,
                family=family,
                limit=limit,
            )
            if query and not cards:
                fallback_used = True
                cards = self._query_cards(
                    requested=requested,
                    query=None,
                    campaign=campaign,
                    family=family,
                    limit=limit,
                )

        event_id = f"MEMRET-{uuid.uuid4().hex[:24]}"
        filters = {
            "campaign": campaign,
            "family": family,
            "requested_kinds": sorted(kind.value for kind in requested),
            "role_allowed_kinds": sorted(kind.value for kind in allowed),
            "excluded_stage": ResearchStage.C0_CONFIRMATION.value,
            "visibility": "DEVELOPMENT",
            "fallback_without_fts": fallback_used,
        }
        card_keys = [f"{card.memory_id}:{card.version}" for card in cards]
        with self._connect() as connection:
            values = (
                role.value,
                query,
                json.dumps(filters, sort_keys=True),
                json.dumps(card_keys, sort_keys=True),
            )
            connection.execute(
                """
                INSERT INTO memory_retrieval_events(
                    retrieval_event_id, role, query, filters_json,
                    returned_card_keys_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, *values),
            )
        return RoleScopedMemoryPacket(
            retrieval_event_id=event_id,
            role=role,
            query=query,
            cards=cards,
        )

    def _query_cards(
        self,
        *,
        requested: frozenset[MemoryKind] | set[MemoryKind],
        query: str | None,
        campaign: str | None,
        family: str | None,
        limit: int,
    ) -> list[ScientificMemoryCard]:
        clauses = [
            "mc.status = ?",
            "mc.visibility = 'DEVELOPMENT'",
            "mc.authority = 'SCHEDULING_ONLY'",
            "mc.stage != ?",
        ]
        parameters: list[object] = [
            MemoryStatus.ACTIVE_SCHEDULING_MEMORY.value,
            ResearchStage.C0_CONFIRMATION.value,
        ]
        placeholders = ",".join("?" for _ in requested)
        clauses.append(f"mc.kind IN ({placeholders})")
        parameters.extend(sorted(kind.value for kind in requested))
        if campaign is not None:
            clauses.append("mc.campaign = ?")
            parameters.append(campaign)
        if family is not None:
            clauses.append("mc.family = ?")
            parameters.append(family)

        fts = _fts_query(query) if query else None
        if fts:
            sql = (
                "SELECT mc.card_json FROM memory_cards AS mc "
                "INNER JOIN memory_cards_fts "
                "ON memory_cards_fts.card_key = mc.memory_id || ':' || mc.version "
                f"WHERE {' AND '.join(clauses)} "
                "AND memory_cards_fts MATCH ? "
                "ORDER BY bm25(memory_cards_fts), mc.created_at_utc DESC "
                "LIMIT ?"
            )
            parameters.extend([fts, max(limit * 4, limit)])
        else:
            sql = (
                "SELECT mc.card_json FROM memory_cards AS mc "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY mc.created_at_utc DESC, mc.memory_id "
                "LIMIT ?"
            )
            parameters.append(max(limit * 4, limit))
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        ranked = [ScientificMemoryCard.model_validate_json(row[0]) for row in rows]
        # Preserve at least one card per available kind before filling by rank.
        selected: list[ScientificMemoryCard] = []
        selected_keys: set[tuple[str, int]] = set()
        for kind in sorted(requested, key=lambda item: item.value):
            match = next((card for card in ranked if card.kind is kind), None)
            if match is not None and len(selected) < limit:
                selected.append(match)
                selected_keys.add((match.memory_id, match.version))
        for card in ranked:
            key = (card.memory_id, card.version)
            if key not in selected_keys and len(selected) < limit:
                selected.append(card)
                selected_keys.add(key)
        return selected
