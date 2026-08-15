from evidence_evolve.governance.closure_registry import (
    ClosureEntry,
    ClosureRegistry,
    audit_candidate_closures,
)
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.scope import audit_paths


def test_deny_wins_over_allow(contract) -> None:
    assert audit_paths(contract.editable_scope, ["evaluators/evaluate.py"]) == [
        "DENIED_PATH:evaluators/evaluate.py"
    ]


def test_outside_allow_is_rejected(contract) -> None:
    assert audit_paths(contract.editable_scope, ["README.md"]) == [
        "OUTSIDE_ALLOW_SCOPE:README.md"
    ]


def test_closed_family_requires_external_reopen_evidence(candidate) -> None:
    candidate.family = "query_local_ray_plane"
    candidate.reopen_condition_claims = ["NEW_NATIVE_TRUTH"]
    registry = ClosureRegistry(
        closures=[
            ClosureEntry(
                closure_id="CLOSE_QPLANE",
                family="query_local_ray_plane",
                status="VALID_NEGATIVE",
                reopen_conditions=["NEW_NATIVE_TRUTH"],
                scope="test",
            )
        ]
    )
    rejected = audit_candidate_closures(candidate, registry)
    assert not rejected.allowed
    admitted = audit_candidate_closures(
        candidate, registry, verified_reopen_conditions={"NEW_NATIVE_TRUTH"}
    )
    assert admitted.allowed
    assert admitted.satisfied_conditions == ["NEW_NATIVE_TRUTH"]


def test_renamed_closed_family_pattern_is_rejected(candidate) -> None:
    candidate.family = "generic_scale_correction_v17"
    registry = ClosureRegistry(
        closures=[
            ClosureEntry(
                closure_id="CLOSE_SCALE",
                family="generic_scale_correction*",
                status="CLOSED",
                scope="test",
            )
        ]
    )
    assert audit_candidate_closures(candidate, registry).violations == [
        "CLOSED_FAMILY:CLOSE_SCALE"
    ]


def test_candidate_vocabulary_must_match_frozen_contract(contract, candidate) -> None:
    candidate.required_controls = ["invented_control"]
    candidate.expected_signature.improve = ["invented_metric"]
    audit = audit_candidate(contract, candidate, ClosureRegistry())
    assert not audit.valid
    assert "FROZEN_REQUIRED_CONTROL_MISSING:wrong_factor" in audit.violations
    assert "FROZEN_REQUIRED_CONTROL_MISSING:zero_factor" in audit.violations
    assert "REQUIRED_CONTROL_NOT_FROZEN:invented_control" in audit.violations
    assert "EXPECTED_SIGNATURE_METRIC_NOT_FROZEN:invented_metric" in audit.violations
