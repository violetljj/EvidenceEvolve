# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure, positive motion, and looming-conditioned corridor risk."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    m = min(a, e)
    p = f["path_intrusion"]
    score = (
        0.30 * f["relative_nearness"] + 0.30 * a + 0.23 * e
        + 0.16 * p + 0.02 * p * m
        + 0.05 * (f["depth_expansion_consistency"]
                  + f["observation_quality"] - 1.0) * m
    )
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

