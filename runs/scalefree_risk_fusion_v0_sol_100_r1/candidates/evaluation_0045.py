from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse interacting exposure and quality-gated motion with a noisy-OR."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    a = min(1.0, max(0.0, f["depth_approach_rate"]))
    e = min(1.0, max(0.0, f["local_expansion"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))

    weak = min(a, e)
    strong = max(a, e)
    exposure = 0.38 * n + 0.22 * p + 0.40 * n * p
    motion = (0.28 * strong + 0.52 * weak + 0.20 * c * weak) \
             * (0.70 + 0.30 * q)

    risk = 1.0 - (1.0 - 0.75 * exposure) * (1.0 - 0.75 * motion)
    risk *= 0.75 + 0.25 * risk
    return min(1.0, max(0.0, risk))
# EVOLVE-BLOCK-END

