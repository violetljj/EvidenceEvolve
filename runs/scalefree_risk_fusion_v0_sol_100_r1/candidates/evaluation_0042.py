# EVOLVE-BLOCK-START
def compute_risk(f):
    """Blend visual exposure and positive motion, boosting reliable corroboration."""
    n = f["relative_nearness"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    p = f["path_intrusion"]
    c = f["depth_expansion_consistency"]
    q = f["observation_quality"]

    exposure = 0.65 * n + 0.35 * p
    motion = 0.55 * a + 0.45 * e
    risk = 0.45 * exposure + 0.55 * motion
    risk += 0.10 * min(exposure, motion) * (0.50 + 0.25 * c + 0.25 * q)
    return min(1.0, max(0.0, risk))
# EVOLVE-BLOCK-END

