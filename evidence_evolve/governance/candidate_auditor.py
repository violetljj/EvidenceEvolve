from __future__ import annotations

from pydantic import Field

from evidence_evolve.governance.closure_registry import (
    ClosureRegistry,
    audit_candidate_closures,
)
from evidence_evolve.governance.scope import audit_paths
from evidence_evolve.models import CandidateGenome, ResearchContract, StrictModel


class CandidateAudit(StrictModel):
    valid: bool
    violations: list[str] = Field(default_factory=list)
    matched_closures: list[str] = Field(default_factory=list)
    satisfied_reopen_conditions: list[str] = Field(default_factory=list)


def audit_candidate(
    contract: ResearchContract,
    candidate: CandidateGenome,
    registry: ClosureRegistry,
    *,
    changed_files: list[str] | None = None,
    verified_reopen_conditions: set[str] | None = None,
) -> CandidateAudit:
    paths = changed_files if changed_files is not None else candidate.editable_files
    violations = audit_paths(contract.editable_scope, paths)
    declared_scope_violations = audit_paths(
        contract.editable_scope, candidate.editable_files
    )
    violations.extend(
        f"DECLARED_{violation}" for violation in declared_scope_violations
    )
    frozen_controls = set(contract.required_controls)
    declared_controls = set(candidate.required_controls)
    violations.extend(
        f"REQUIRED_CONTROL_NOT_FROZEN:{control}"
        for control in sorted(declared_controls - frozen_controls)
    )
    violations.extend(
        f"FROZEN_REQUIRED_CONTROL_MISSING:{control}"
        for control in sorted(frozen_controls - declared_controls)
    )
    frozen_metrics = set(contract.metrics.pareto_objectives)
    declared_metrics = set(candidate.expected_signature.improve) | set(
        candidate.expected_signature.unchanged
    )
    violations.extend(
        f"EXPECTED_SIGNATURE_METRIC_NOT_FROZEN:{metric}"
        for metric in sorted(declared_metrics - frozen_metrics)
    )
    closure = audit_candidate_closures(
        candidate,
        registry,
        verified_reopen_conditions=verified_reopen_conditions,
    )
    violations.extend(closure.violations)
    return CandidateAudit(
        valid=not violations,
        violations=sorted(set(violations)),
        matched_closures=closure.matched_closures,
        satisfied_reopen_conditions=closure.satisfied_conditions,
    )
