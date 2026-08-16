# EVOLVE-BLOCK-START
import math


def compute_risk(f):
    """Geometrically fuse exposure and motion with protected safety floors."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    d = min(1.0, max(-1.0, f["depth_approach_rate"]))
    x = min(1.0, max(-1.0, f["local_expansion"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))

    a = max(0.0, d)
    e = max(0.0, x)
    exposure = 0.66 * n + 0.34 * p
    motion = 0.52 * a + 0.43 * e
    motion += 0.05 * math.sqrt(a * e) * (0.5 + 0.25 * c + 0.25 * q)

    joint = math.sqrt(exposure * motion) * (0.90 + 0.05 * c + 0.05 * q)
    core = 0.34 * exposure + 0.46 * motion + 0.20 * joint

    retreat = max(0.0, -d) * max(0.0, -x) * (1.0 - p)
    core *= 1.0 - 0.10 * retreat

    risk = max(core, 0.455 * exposure, 0.58 * motion)
    return min(1.0, max(0.0, risk))
# EVOLVE-BLOCK-END

