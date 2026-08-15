from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from evidence_evolve.models import MutationType, ScientificOutcome, StrictModel
from evidence_evolve.research_memory import (
    MemoryKind,
    RoleScopedMemoryPacket,
)


class ResearchAction(StrEnum):
    MUTATE = "MUTATE"
    REPLICATE = "REPLICATE"
    ABLATE = "ABLATE"
    FALSIFY = "FALSIFY"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"
    TRANSFER = "TRANSFER"
    SIMPLIFY = "SIMPLIFY"
    UNDERSTAND = "UNDERSTAND"
    SEARCH_LITERATURE = "SEARCH_LITERATURE"
    REPRODUCE = "REPRODUCE"
    ACQUIRE_EVIDENCE = "ACQUIRE_EVIDENCE"
    CLOSE = "CLOSE"
    REOPEN = "REOPEN"
    BREAKTHROUGH = "BREAKTHROUGH"


class ResearchDirectorDecision(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    generation_id: str
    primary_action: ResearchAction
    executable_action: ResearchAction
    rationale: list[str] = Field(min_length=1)
    evidence_memory_ids: list[str] = Field(default_factory=list)
    recommended_mutation_mix: dict[MutationType, float]
    blocked_actions: dict[ResearchAction, str] = Field(default_factory=dict)
    authority: Literal["SCHEDULING_ONLY"] = "SCHEDULING_ONLY"

    @model_validator(mode="after")
    def mutation_mix_is_a_distribution(self) -> "ResearchDirectorDecision":
        if not self.recommended_mutation_mix:
            raise ValueError("recommended_mutation_mix cannot be empty")
        if any(value < 0 for value in self.recommended_mutation_mix.values()):
            raise ValueError("recommended mutation weights cannot be negative")
        if abs(sum(self.recommended_mutation_mix.values()) - 1.0) > 1e-9:
            raise ValueError("recommended mutation weights must sum to 1.0")
        return self


def research_action_for_mutation(mutation: MutationType) -> ResearchAction:
    return {
        MutationType.MECHANISM: ResearchAction.MUTATE,
        MutationType.REPRESENTATION: ResearchAction.BREAKTHROUGH,
        MutationType.FAILURE_DIRECTED: ResearchAction.COUNTEREXAMPLE,
        MutationType.CONTROL: ResearchAction.FALSIFY,
        MutationType.SIMPLIFICATION: ResearchAction.SIMPLIFY,
        MutationType.CROSS_FAMILY: ResearchAction.TRANSFER,
        MutationType.RESTART: ResearchAction.BREAKTHROUGH,
    }[mutation]


class ResearchDirector:
    """Turn evidence-bound memory into an auditable next-action recommendation.

    This is a scheduling component, not a scientist or gate. It can change search
    allocation but cannot write observations, verdicts, or claim authority.
    """

    def decide(
        self,
        *,
        generation_id: str,
        packet: RoleScopedMemoryPacket,
        stagnant_generations: int,
        stagnation_threshold: int,
        default_mix: dict[MutationType, float],
        breakthrough_mix: dict[MutationType, float],
    ) -> ResearchDirectorDecision:
        cards = packet.cards
        evidence_ids = list(dict.fromkeys(card.memory_id for card in cards))
        failure_cards = [card for card in cards if card.kind is MemoryKind.FAILURE]
        frontier_cards = [card for card in cards if card.kind is MemoryKind.FRONTIER]
        supported = [
            card
            for card in cards
            if card.kind is MemoryKind.MECHANISM
            and card.epistemics.scientific_outcome
            is ScientificOutcome.POSITIVE_HEADROOM
        ]
        family_counts = Counter(card.scope.family for card in failure_cards)
        saturated_family = None
        if len(failure_cards) >= 3 and family_counts:
            family, count = family_counts.most_common(1)[0]
            if count / len(failure_cards) >= 0.6:
                saturated_family = family

        if stagnant_generations >= stagnation_threshold or saturated_family:
            reasons = [
                "Local search is saturated or the frozen stagnation threshold was reached",
                "Allocate search to representation, cross-family, and restart mutations",
            ]
            if saturated_family:
                reasons.append(f"Repeated failure family: {saturated_family}")
            return ResearchDirectorDecision(
                generation_id=generation_id,
                primary_action=ResearchAction.BREAKTHROUGH,
                executable_action=ResearchAction.BREAKTHROUGH,
                rationale=reasons,
                evidence_memory_ids=evidence_ids,
                recommended_mutation_mix=breakthrough_mix,
            )

        unresolved_count = sum(
            len(card.content.unresolved_questions) for card in frontier_cards
        )
        if frontier_cards and unresolved_count:
            return ResearchDirectorDecision(
                generation_id=generation_id,
                primary_action=ResearchAction.ABLATE,
                executable_action=ResearchAction.ABLATE,
                rationale=[
                    f"{unresolved_count} evidence-bound unresolved items remain",
                    "Prefer a cheap discriminating control or simplification before broad mutation",
                ],
                evidence_memory_ids=evidence_ids,
                recommended_mutation_mix={
                    MutationType.CONTROL: 0.5,
                    MutationType.SIMPLIFICATION: 0.25,
                    MutationType.FAILURE_DIRECTED: 0.25,
                },
            )

        if supported:
            return ResearchDirectorDecision(
                generation_id=generation_id,
                primary_action=ResearchAction.FALSIFY,
                executable_action=ResearchAction.FALSIFY,
                rationale=[
                    "A positive development result exists",
                    "Attack its mechanism with controls before allocating more local optimization",
                ],
                evidence_memory_ids=evidence_ids,
                recommended_mutation_mix={
                    MutationType.CONTROL: 0.6,
                    MutationType.SIMPLIFICATION: 0.2,
                    MutationType.FAILURE_DIRECTED: 0.2,
                },
            )

        if not cards:
            return ResearchDirectorDecision(
                generation_id=generation_id,
                primary_action=ResearchAction.SEARCH_LITERATURE,
                executable_action=ResearchAction.MUTATE,
                rationale=[
                    "No eligible internal research memory was retrieved",
                    "External knowledge acquisition is preferred before repeated local search",
                    "The current runner falls back to one bounded mutation until a literature executor is wired",
                ],
                recommended_mutation_mix=default_mix,
                blocked_actions={
                    ResearchAction.SEARCH_LITERATURE: (
                        "No source-bound literature/repository action executor is wired yet"
                    )
                },
            )

        return ResearchDirectorDecision(
            generation_id=generation_id,
            primary_action=ResearchAction.MUTATE,
            executable_action=ResearchAction.MUTATE,
            rationale=[
                "No saturation or unresolved-question trigger dominates the current evidence",
                "Continue the frozen policy mix with memory-grounded proposal context",
            ],
            evidence_memory_ids=evidence_ids,
            recommended_mutation_mix=default_mix,
        )
