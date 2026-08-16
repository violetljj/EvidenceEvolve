from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(features: dict[str, float]) -> float:
    """Blend exposure with a conservative reliability correction to corroborated motion."""
    approach = max(0.0, features["depth_approach_rate"])
    expansion = max(0.0, features["local_expansion"])
    return min(1.0,
        0.30 * features["relative_nearness"] + 0.30 * approach
        + 0.25 * expansion + 0.15 * features["path_intrusion"]
        + 0.02 * (features["depth_expansion_consistency"]
                  + features["observation_quality"] - 1.0)
        * min(approach, expansion))
# EVOLVE-BLOCK-END

