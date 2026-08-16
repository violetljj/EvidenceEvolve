from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and corroborated motion, emphasizing corridor relevance."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0, 0.28 * f["relative_nearness"] + 0.30 * a + 0.25 * e
        + 0.17 * f["path_intrusion"] + 0.04 * min(a, e)
        * (f["depth_expansion_consistency"] + f["observation_quality"] - 1.0))
# EVOLVE-BLOCK-END

