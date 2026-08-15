from __future__ import annotations

from evidence_evolve.benchmarks.models import (
    ArmTrialSubmission,
    BenchmarkTrialContext,
)


def scripted_protocol_smoke(context: BenchmarkTrialContext) -> ArmTrialSubmission:
    """Exercise paired orchestration without simulating research superiority.

    Every arm deliberately submits the exact same frozen baseline. Any non-zero
    arm delta therefore indicates a benchmark orchestration defect.
    """
    return ArmTrialSubmission(
        executor_id="scripted-protocol-smoke-v1",
        candidate_paths=["tasks/graph_coloring/candidates/baseline.py"],
        proposal_calls_used=0,
        token_count_used=0,
        metadata={
            "authority": "MECHANICS_ONLY",
            "arm": context.arm.value,
            "suite_id": context.suite_id,
        },
    )
