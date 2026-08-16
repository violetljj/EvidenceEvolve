# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion, then correct corroborated and ambiguous regimes."""
    n = f["relative_nearness"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    p = f["path_intrusion"]
    c = f["depth_expansion_consistency"]
    q = f["observation_quality"]

    exposure = 0.64 * n + 0.36 * p
    motion = 0.57 * a + 0.43 * e
    base = 0.47 * exposure + 0.53 * motion

    corroborated = min(exposure, min(a, e))
    rescue = 0.045 * c * (0.5 + 0.5 * q) * corroborated * (1.0 - base)
    ambiguity = ((1.0 - c) * (1.0 - q) * abs(a - e)
                 * (1.0 - exposure) * (1.0 - min(exposure, motion)))
    risk = base + rescue - 0.015 * ambiguity * base
    return min(1.0, max(0.0, risk))
# EVOLVE-BLOCK-END

