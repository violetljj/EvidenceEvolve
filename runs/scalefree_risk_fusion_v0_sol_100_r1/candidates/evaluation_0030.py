# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure, corridor relevance, and quality-gated motion agreement."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    a = min(1.0, max(0.0, f["depth_approach_rate"]))
    e = min(1.0, max(0.0, f["local_expansion"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))
    motion = min(a, e)
    corridor = (p - 0.5) * (n + min(1.0, a + e))
    return min(1.0, max(0.0,
        0.30 * n + 0.30 * a + 0.25 * e + 0.15 * p
        + 0.04 * q * (2.0 * c - 1.0) * motion
        + 0.025 * corridor))
# EVOLVE-BLOCK-END

