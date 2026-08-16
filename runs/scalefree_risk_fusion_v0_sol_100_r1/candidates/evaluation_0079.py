# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and positive motion with reliability-corrected agreement."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    agreement = min(a, e)
    score = (0.29 * f["relative_nearness"] + 0.31 * a + 0.23 * e
             + 0.17 * f["path_intrusion"]
             + 0.04 * agreement * (f["depth_expansion_consistency"]
                                   + f["observation_quality"] - 1.0))
    return max(0.0, min(1.0, score))
# EVOLVE-BLOCK-END

