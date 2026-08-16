# EVOLVE-BLOCK-START
def compute_risk(f):
    """Blend exposure and positive motion, reliability-correcting their balance."""
    n = f["relative_nearness"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    p = f["path_intrusion"]
    b = max(n, p)
    r = 0.75 * f["depth_expansion_consistency"] + 0.25 * f["observation_quality"]
    s = (0.30 * n + 0.26 * a + 0.21 * e + 0.15 * p
         + 0.08 * (b + r * (0.5 * (a + e) - b)))
    return min(1.0, max(0.0, s))
# EVOLVE-BLOCK-END

