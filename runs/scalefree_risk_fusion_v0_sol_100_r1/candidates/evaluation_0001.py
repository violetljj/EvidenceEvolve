from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(features: dict[str, float]) -> float:
    """Fuse static exposure with reliability-gated, mutually consistent motion evidence."""

    near = features["relative_nearness"]
    approach = max(0.0, features["depth_approach_rate"])
    expansion = max(0.0, features["local_expansion"])
    intrusion = features["path_intrusion"]
    motion = 0.5 * (approach + expansion)
    reliability = (
        0.75 * features["depth_expansion_consistency"]
        + 0.25 * features["observation_quality"]
    )
    fallback = max(near, intrusion)

    score = (
        0.30 * near
        + 0.26 * approach
        + 0.21 * expansion
        + 0.15 * intrusion
        + 0.08 * (reliability * motion + (1.0 - reliability) * fallback)
    )
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

