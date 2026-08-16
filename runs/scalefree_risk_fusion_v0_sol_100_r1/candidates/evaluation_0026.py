from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion with exposure-directed reliability correction."""
    n = f["relative_nearness"]
    p = f["path_intrusion"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    exposure = 0.5 * (n + p)
    reliability = f["depth_expansion_consistency"] + f["observation_quality"] - 1.0
    gain = 0.02 + 0.04 * (exposure if reliability >= 0.0 else 1.0 - exposure)
    score = 0.30 * n + 0.30 * a + 0.25 * e + 0.15 * p
    score += gain * reliability * min(a, e)
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

