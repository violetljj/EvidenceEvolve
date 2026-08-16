# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and positive motion with stronger reliability-weighted corroboration."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0,
        0.30 * f["relative_nearness"] + 0.30 * a + 0.23 * e
        + 0.17 * f["path_intrusion"]
        + 0.05 * (f["depth_expansion_consistency"]
                  + f["observation_quality"] - 1.0) * min(a, e))
# EVOLVE-BLOCK-END

