# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with reliable joint motion, centered on corridor relevance."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0, max(0.0,
        0.30 * f["relative_nearness"] + 0.30 * a + 0.25 * e
        + 0.15 * f["path_intrusion"]
        + 0.04 * (f["depth_expansion_consistency"]
                  + f["observation_quality"]
                  + f["path_intrusion"] - 1.35) * min(a, e)))
# EVOLVE-BLOCK-END

