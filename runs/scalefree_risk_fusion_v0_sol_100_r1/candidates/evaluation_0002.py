from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(features: dict[str, float]) -> float:
    """Fuse exposure with confidence-weighted, consistent positive motion."""

    n = features["relative_nearness"]
    a = max(0.0, features["depth_approach_rate"])
    e = max(0.0, features["local_expansion"])
    p = features["path_intrusion"]
    b = max(n, p)
    r = 0.75 * features["depth_expansion_consistency"] + 0.25 * features["observation_quality"]
    s = 0.30 * n + 0.26 * a + 0.21 * e + 0.15 * p + 0.08 * (b + r * (0.5 * (a + e) - b))
    return min(1.0, max(0.0, s))
# EVOLVE-BLOCK-END

