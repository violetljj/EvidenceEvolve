from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with reliability-gated corroborated positive motion."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0, 0.30 * (f["relative_nearness"] + a) + 0.25 * e
        + 0.15 * f["path_intrusion"] + 0.04 * min(a, e)
        * (f["depth_expansion_consistency"] + f["observation_quality"] - 1.0))
# EVOLVE-BLOCK-END

