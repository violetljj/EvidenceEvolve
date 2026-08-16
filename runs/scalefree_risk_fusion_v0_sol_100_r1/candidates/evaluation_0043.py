# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with corridor-gated motion and reliable corroboration."""
    n = f["relative_nearness"]
    p = f["path_intrusion"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    c = f["depth_expansion_consistency"]
    q = f["observation_quality"]
    x = 0.64 * n + 0.36 * p
    m = 0.52 * a + 0.48 * e
    r = 0.43 * x + (0.45 + 0.08 * p) * m
    r += 0.09 * min(x, m) * (0.50 + 0.30 * c + 0.20 * q)
    return min(1.0, max(0.0, r))
# EVOLVE-BLOCK-END

