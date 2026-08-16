# EVOLVE-BLOCK-START
def compute_risk(f):
    """Blend exposure and positive motion, then add reliability-weighted agreement."""
    a = max(0.0, min(1.0, f["depth_approach_rate"]))
    e = max(0.0, min(1.0, f["local_expansion"]))
    n = max(0.0, min(1.0, f["relative_nearness"]))
    p = max(0.0, min(1.0, f["path_intrusion"]))
    c = max(0.0, min(1.0, f["depth_expansion_consistency"]))
    q = max(0.0, min(1.0, f["observation_quality"]))
    exposure = (2.0 * n + p) / 3.0
    motion = (6.0 * a + 5.0 * e) / 11.0
    agreement = min(exposure, motion) * (0.5 + 0.25 * c + 0.25 * q)
    return max(0.0, min(1.0, 0.45 * exposure + 0.45 * motion
                        + 0.10 * agreement))
# EVOLVE-BLOCK-END

