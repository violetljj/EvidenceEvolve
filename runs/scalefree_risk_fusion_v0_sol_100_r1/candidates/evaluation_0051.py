from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse corridor exposure with quality-weighted motion corroboration."""
    n, a, e, p, c, q = (f[k] for k in (
        "relative_nearness", "depth_approach_rate", "local_expansion",
        "path_intrusion", "depth_expansion_consistency", "observation_quality"))
    x = n + .55 * p * (1 - n)
    lo = min(a, e)
    motion = lo + abs(a - e) * (.42 + .1 * q)
    motion += .18 * c * lo * (1 - motion)
    risk = .34 * x + .45 * motion + .23 * x * motion
    return min(1, max(0, risk))
# EVOLVE-BLOCK-END

