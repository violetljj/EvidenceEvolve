from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse saturating exposure with reliability-gated motion and a safety floor."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    a = min(1.0, max(0.0, f["depth_approach_rate"]))
    e = 0.77 * min(1.0, max(0.0, f["local_expansion"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))

    exposure = n + 0.55 * p * (1.0 - n)
    lo, hi = min(a, e), max(a, e)
    motion = lo + (hi - lo) * (0.45 + 0.15 * c + 0.10 * q)
    fused = 0.34 * exposure + 0.45 * motion + 0.23 * exposure * motion
    safety = 0.24 * n + 0.13 * p + 0.18 * n * p
    return min(1.0, max(0.0, fused, safety))
# EVOLVE-BLOCK-END

