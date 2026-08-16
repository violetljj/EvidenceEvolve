from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(features: dict[str, float]) -> float:
    """Blend exposure and positive motion with a corroborated-risk rescue."""

    approach = max(0.0, features["depth_approach_rate"])
    expansion = max(0.0, features["local_expansion"])
    intrusion = features["path_intrusion"]
    score = (
        0.30 * features["relative_nearness"]
        + 0.30 * approach
        + 0.25 * expansion
        + 0.15 * intrusion
        + 0.035
        * min(approach, expansion)
        * features["depth_expansion_consistency"]
        * features["observation_quality"]
        * (0.5 + 0.5 * intrusion)
    )
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

