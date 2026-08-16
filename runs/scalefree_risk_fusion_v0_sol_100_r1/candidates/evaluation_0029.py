from __future__ import annotations

from math import sqrt


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse balanced exposure-motion evidence with a strong-cue safety floor."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    a = min(1.0, max(0.0, f["depth_approach_rate"]))
    e = min(1.0, max(0.0, f["local_expansion"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))

    paired = min(a, e)
    motion = 0.34 * (a + e) + 0.32 * paired
    motion = min(1.0, max(0.0, motion + 0.10 * q * (c - 0.5) * paired))
    exposure = 0.68 * n + 0.32 * p

    linear = 0.45 * exposure + 0.55 * motion
    balanced = 0.60 * linear + 0.40 * sqrt(exposure * motion)
    safety = 0.30 * n + 0.14 * p + 0.36 * max(a, e) + 0.20 * paired
    return min(1.0, max(0.0, max(balanced, safety)))
# EVOLVE-BLOCK-END

