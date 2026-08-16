# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion, adding a corridor-corroborated safety boost."""
    n = f["relative_nearness"]
    p = f["path_intrusion"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    score = (0.30 * n + 0.30 * a + 0.23 * e + 0.17 * p
             + 0.04 * (f["depth_expansion_consistency"]
                       + f["observation_quality"] - 1.0) * min(a, e)
             + 0.03 * min(n, p, max(a, e)))
    return max(0.0, min(1.0, score))
# EVOLVE-BLOCK-END

