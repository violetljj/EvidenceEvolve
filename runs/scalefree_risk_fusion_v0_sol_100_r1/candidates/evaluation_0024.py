from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with conservative, jointly reliable motion corroboration."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    r = f["depth_expansion_consistency"] * f["observation_quality"]
    return min(1.0, max(0.0, 0.30 * (f["relative_nearness"] + a)
        + 0.25 * e + 0.15 * f["path_intrusion"]
        + 0.05 * min(a, e) * (r - 0.20)))
# EVOLVE-BLOCK-END

