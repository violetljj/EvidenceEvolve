# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion, discounting corridor evidence only when motion is weak."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    p = f["path_intrusion"]
    m = max(a, e)
    return min(1.0,
        0.30 * f["relative_nearness"] + 0.30 * a + 0.23 * e
        + 0.17 * p - 0.02 * p * (1.0 - m) * (1.0 - m)
        + 0.04 * (f["depth_expansion_consistency"]
                  + f["observation_quality"] - 1.0) * min(a, e))
# EVOLVE-BLOCK-END

