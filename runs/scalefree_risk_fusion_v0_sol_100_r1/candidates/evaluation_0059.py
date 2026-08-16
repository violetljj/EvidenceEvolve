# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion, conservatively resolving unreliable disagreement."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    n = f["relative_nearness"]
    p = f["path_intrusion"]
    reliability = 0.5 * (f["depth_expansion_consistency"]
                         + f["observation_quality"])
    score = (
        0.30 * n + 0.30 * a + 0.23 * e + 0.17 * p
        + 0.04 * (2.0 * reliability - 1.0) * min(a, e)
        + 0.025 * (2.0 * max(n, p) - 1.0)
        * (1.0 - reliability) * abs(a - e)
    )
    return min(1.0, max(0.0, score))
# EVOLVE-BLOCK-END

