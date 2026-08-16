# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse motion evidence with centered near-corridor exposure synergy."""
    n = f["relative_nearness"]
    p = f["path_intrusion"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    score = (0.30 * n + 0.30 * a + 0.225 * e + 0.175 * p
        + 0.045 * (f["depth_expansion_consistency"]
                   + f["observation_quality"] - 1.0) * min(a, e)
        + 0.04 * (n - 0.5) * (p - 0.5))
    return max(0.0, min(1.0, score))
# EVOLVE-BLOCK-END

