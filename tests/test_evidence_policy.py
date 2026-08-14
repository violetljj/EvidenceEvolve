from evidence_evolve.governance.evidence_policy import (
    audit_source,
    permission_allowed,
)
from evidence_evolve.models import (
    EvidenceGrade,
    EvidencePermission,
    EvidenceSource,
)


def test_a_and_b_can_support_claims() -> None:
    assert permission_allowed(EvidenceGrade.A, EvidencePermission.CLAIM)
    assert permission_allowed(EvidenceGrade.B, EvidencePermission.CONFIRM)


def test_c_cannot_confirm_or_claim() -> None:
    assert permission_allowed(EvidenceGrade.C, EvidencePermission.DEV)
    assert not permission_allowed(EvidenceGrade.C, EvidencePermission.CONFIRM)
    assert not permission_allowed(EvidenceGrade.C, EvidencePermission.CLAIM)


def test_d_is_candidate_mining_only() -> None:
    assert permission_allowed(EvidenceGrade.D, EvidencePermission.CANDIDATE_MINING)
    assert not permission_allowed(EvidenceGrade.D, EvidencePermission.TRAIN)


def test_source_permission_escalation_is_reported() -> None:
    source = EvidenceSource(
        source_id="teacher-c",
        grade=EvidenceGrade.C,
        path="manifests/teacher.json",
        permissions={EvidencePermission.DEV, EvidencePermission.CLAIM},
    )
    assert audit_source(source) == [
        "EVIDENCE_PERMISSION_EXCEEDED:teacher-c:grade=C:permissions=CLAIM"
    ]

