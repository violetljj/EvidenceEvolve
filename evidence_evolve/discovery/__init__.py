"""Open-ended discovery orchestration under the frozen constitution."""

from evidence_evolve.discovery.campaign import (
    CampaignCandidate,
    CampaignGenerationResult,
    CampaignRunner,
    EvaluationRun,
)

__all__ = [
    "CampaignCandidate",
    "CampaignGenerationResult",
    "CampaignRunner",
    "EvaluationRun",
]
from evidence_evolve.discovery.autonomous import (
    AutonomousCampaignResult,
    AutonomousCampaignRunner,
    AutonomousEvaluationContext,
    ImplementationManifest,
)

__all__ = [
    "AutonomousCampaignResult",
    "AutonomousCampaignRunner",
    "AutonomousEvaluationContext",
    "ImplementationManifest",
]
