from __future__ import annotations

from evidence_evolve.models import EvidenceGrade, EvidencePermission, EvidenceSource


GRADE_PERMISSIONS: dict[EvidenceGrade, frozenset[EvidencePermission]] = {
    EvidenceGrade.A: frozenset(EvidencePermission),
    EvidenceGrade.B: frozenset(EvidencePermission),
    EvidenceGrade.C: frozenset(
        {EvidencePermission.TRAIN, EvidencePermission.DEV}
    ),
    EvidenceGrade.D: frozenset({EvidencePermission.CANDIDATE_MINING}),
}


def permission_allowed(grade: EvidenceGrade, permission: EvidencePermission) -> bool:
    return permission in GRADE_PERMISSIONS[grade]


def audit_source(source: EvidenceSource) -> list[str]:
    invalid = sorted(
        permission.value
        for permission in source.permissions
        if not permission_allowed(source.grade, permission)
    )
    if not invalid:
        return []
    return [
        f"EVIDENCE_PERMISSION_EXCEEDED:{source.source_id}:"
        f"grade={source.grade.value}:permissions={','.join(invalid)}"
    ]


def eligible_sources(
    sources: list[EvidenceSource], permission: EvidencePermission
) -> list[EvidenceSource]:
    return [source for source in sources if permission in source.permissions]

