from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(features: dict[str, float]) -> float:
    """Blend exposure with extra weight on corroborating positive motion."""

    score = (
        0.28 * features["relative_nearness"]
        + 0.32 * max(0.0, features["depth_approach_rate"])
        + 0.27 * max(0.0, features["local_expansion"])
        + 0.13 * features["path_intrusion"]
    )
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

