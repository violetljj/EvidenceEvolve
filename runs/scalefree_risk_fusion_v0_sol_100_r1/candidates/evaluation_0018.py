from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(features: dict[str, float]) -> float:
    """Correct baseline risk toward exposure when motion evidence is unreliable."""

    n = features["relative_nearness"]
    a = max(0.0, features["depth_approach_rate"])
    e = max(0.0, features["local_expansion"])
    p = features["path_intrusion"]
    reliability = (
        0.75 * features["depth_expansion_consistency"]
        + 0.25 * features["observation_quality"]
    )
    score = 0.30 * n + 0.30 * a + 0.25 * e + 0.15 * p
    score += 0.04 * (1.0 - reliability) * (max(n, p) - 0.5 * (a + e))
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

