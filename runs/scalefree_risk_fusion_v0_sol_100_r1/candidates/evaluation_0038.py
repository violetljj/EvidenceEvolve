# EVOLVE-BLOCK-START
def compute_risk(f):
    """Blend exposure and motion, rewarding reliable approach–looming agreement."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    corroboration = (f["observation_quality"]
                     * f["depth_expansion_consistency"] * min(a, e))
    score = (0.28 * f["relative_nearness"] + 0.28 * a + 0.23 * e
             + 0.14 * f["path_intrusion"] + 0.07 * corroboration)
    return max(0.0, min(1.0, score))
# EVOLVE-BLOCK-END

