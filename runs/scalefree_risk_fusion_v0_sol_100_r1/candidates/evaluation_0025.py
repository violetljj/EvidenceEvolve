# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with quality-gated joint approach and looming evidence."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0,
        0.30 * f["relative_nearness"] + 0.30 * a + 0.25 * e
        + 0.15 * f["path_intrusion"]
        + 0.05 * (f["depth_expansion_consistency"]
                  + f["observation_quality"] - 1.0) * a * e)
# EVOLVE-BLOCK-END

