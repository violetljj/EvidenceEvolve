from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with corroboration-boosted positive motion."""
    n, a, e, p, c, q = (min(1, max(0, f[k])) for k in (
        "relative_nearness", "depth_approach_rate", "local_expansion",
        "path_intrusion", "depth_expansion_consistency", "observation_quality"))
    e *= .77
    x = n + .55 * p * (1 - n)
    lo, hi = min(a, e), max(a, e)
    base = lo + (hi - lo) * (.42 + .1 * q)
    motion = base + .18 * c * lo * (1 - base)
    fused = .34 * x + .45 * motion + .23 * x * motion
    safety = .24 * n + .13 * p + .18 * n * p
    return min(1, max(0, fused, safety))
# EVOLVE-BLOCK-END

