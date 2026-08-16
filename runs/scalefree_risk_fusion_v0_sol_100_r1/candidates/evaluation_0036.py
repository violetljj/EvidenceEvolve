from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse bounded exposure, corridor motion, and corroborated looming regimes."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    a = min(1.0, max(0.0, f["depth_approach_rate"]))
    e = min(1.0, max(0.0, f["local_expansion"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))

    exposure = 0.36 * n + 0.12 * p + 0.10 * n * p
    motion = (0.25 * a + 0.19 * e) * (0.70 + 0.30 * q) * (0.72 + 0.28 * p)
    looming = 0.13 * min(a, e) * (0.45 + 0.55 * c) * (0.55 + 0.45 * q)
    fused = 1.0 - (1.0 - exposure) * (1.0 - motion) * (1.0 - looming)

    rescue = p * (0.20 * n + 0.24 * min(a, e) * (0.60 + 0.40 * c))
    return min(1.0, max(fused, rescue))
# EVOLVE-BLOCK-END

