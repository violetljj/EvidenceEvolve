# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion, selectively rescuing reliable corridor looming."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    p = f["path_intrusion"]
    c = f["depth_expansion_consistency"]
    q = f["observation_quality"]
    agreement = min(a, e)
    risk = (0.30 * f["relative_nearness"] + 0.30 * a + 0.23 * e
            + 0.17 * p + 0.035 * (c + q - 1.0) * agreement
            + 0.025 * p * agreement * (0.5 + 0.5 * c)
            * (0.5 + 0.5 * q))
    return max(0.0, min(1.0, risk))
# EVOLVE-BLOCK-END

