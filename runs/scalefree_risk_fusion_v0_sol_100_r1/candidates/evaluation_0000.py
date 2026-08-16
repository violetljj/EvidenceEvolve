from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(features: dict[str, float]) -> float:
    """Return a scale-free relative-risk score in the closed interval [0, 1]."""

    score = (
        0.30 * features["relative_nearness"]
        + 0.30 * max(0.0, features["depth_approach_rate"])
        + 0.25 * max(0.0, features["local_expansion"])
        + 0.15 * features["path_intrusion"]
    )
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

