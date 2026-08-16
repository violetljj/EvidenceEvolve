# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with quality-modulated consistency of corroborated motion."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0,
        0.30 * f["relative_nearness"] + 0.30 * a + 0.25 * e
        + 0.15 * f["path_intrusion"]
        + 0.04 * (f["depth_expansion_consistency"]
                  * (1.0 + f["observation_quality"]) - 0.75) * min(a, e))
# EVOLVE-BLOCK-END

