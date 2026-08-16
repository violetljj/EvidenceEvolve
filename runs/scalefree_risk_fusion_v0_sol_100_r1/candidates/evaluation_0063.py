# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and positive motion with a corridor safety rescue."""
    n, d, l, p, c, q = (f[k] for k in (
        "relative_nearness", "depth_approach_rate", "local_expansion",
        "path_intrusion", "depth_expansion_consistency", "observation_quality"))
    a, e = max(0.0, d), max(0.0, l)
    x = 0.65 * n + 0.35 * p
    m = 0.55 * a + 0.45 * e
    r = (0.45 * x + 0.55 * m + 0.035 * p * max(a, e)
         + 0.025 * (c + q - 1.0) * min(x, m))
    return min(1.0, max(0.0, r))
# EVOLVE-BLOCK-END

