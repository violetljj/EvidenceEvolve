# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and positive motion with reliability-corrected corroboration."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0,
        0.32 * f["relative_nearness"] + 0.30 * a + 0.25 * e
        + 0.13 * f["path_intrusion"]
        + 0.02 * (f["depth_expansion_consistency"]
                  + f["observation_quality"] - 1.0) * min(a, e))
# EVOLVE-BLOCK-END

