from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from evidence_evolve.models import CandidateGenome, StrictModel


class ClosureEntry(StrictModel):
    closure_id: str
    family: str
    status: Literal["CLOSED", "VALID_NEGATIVE"]
    evidence: list[str] = Field(default_factory=list)
    prohibited_mutations: list[str] = Field(default_factory=list)
    reopen_conditions: list[str] = Field(default_factory=list)
    reopen_policy: Literal["any", "all"] = "any"
    scope: str
    observed_failure: list[str] = Field(default_factory=list)


class ClosureRegistry(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    closures: list[ClosureEntry] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "ClosureRegistry":
        with path.open("r", encoding="utf-8") as stream:
            payload = yaml.safe_load(stream) or {}
        return cls.model_validate(payload)


class ClosureAudit(StrictModel):
    allowed: bool
    matched_closures: list[str] = Field(default_factory=list)
    satisfied_conditions: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


def _family_matches(candidate_family: str, closed_family: str) -> bool:
    return fnmatchcase(candidate_family, closed_family) or fnmatchcase(
        closed_family, candidate_family
    )


def audit_candidate_closures(
    candidate: CandidateGenome,
    registry: ClosureRegistry,
    verified_reopen_conditions: set[str] | None = None,
) -> ClosureAudit:
    verified = verified_reopen_conditions or set()
    matched: list[str] = []
    satisfied: list[str] = []
    violations: list[str] = []

    for closure in registry.closures:
        mutation_blocked = any(
            fnmatchcase(candidate.mutation_type.value, pattern)
            for pattern in closure.prohibited_mutations
        )
        if not _family_matches(candidate.family, closure.family) and not mutation_blocked:
            continue
        matched.append(closure.closure_id)
        required = set(closure.reopen_conditions)
        claimed = set(candidate.reopen_condition_claims)
        externally_supported = required & claimed & verified
        reopened = False
        if required:
            if closure.reopen_policy == "all":
                reopened = required <= externally_supported
            else:
                reopened = bool(externally_supported)
        if reopened:
            satisfied.extend(sorted(externally_supported))
        else:
            violations.append(f"CLOSED_FAMILY:{closure.closure_id}")

    return ClosureAudit(
        allowed=not violations,
        matched_closures=sorted(set(matched)),
        satisfied_conditions=sorted(set(satisfied)),
        violations=sorted(set(violations)),
    )

