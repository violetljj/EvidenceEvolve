from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion, discounting only low-confidence ambiguity."""
    n = f["relative_nearness"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    p = f["path_intrusion"]
    c = f["depth_expansion_consistency"]
    q = f["observation_quality"]
    x = 0.38 * n + 0.22 * p + 0.40 * n * p
    w = min(a, e)
    m = 0.28 * max(a, e) + (0.52 + 0.20 * c) * w
    j = min(x, m)
    r = 1.0 - (1.0 - 0.78 * x) * (1.0 - 0.72 * m)
    r += 0.08 * q * j * (1.0 - r)
    r -= 0.18 * (1.0 - j) * (1.0 - 0.5 * q) * r * (1.0 - r)
    return min(1.0, max(0.0, r))
# EVOLVE-BLOCK-END

