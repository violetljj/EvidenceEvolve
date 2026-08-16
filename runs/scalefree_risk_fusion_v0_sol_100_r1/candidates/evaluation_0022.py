# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse a conflict-aware scene estimate with an intrusion-motion safety floor."""
    n = max(0.0, min(1.0, f["relative_nearness"]))
    a = max(0.0, min(1.0, f["depth_approach_rate"]))
    e = max(0.0, min(1.0, f["local_expansion"]))
    p = max(0.0, min(1.0, f["path_intrusion"]))
    c = max(0.0, min(1.0, f["depth_expansion_consistency"]))
    q = max(0.0, min(1.0, f["observation_quality"]))

    exposure = (2.0 * n + p) / 3.0
    disagreement = abs(a - e)
    weak_scene = 1.0 - max(n, p)
    motion = (0.55 * a + 0.45 * e
              + 0.08 * (c - 0.5) * min(a, e)
              - 0.12 * (1.0 - q) * (1.0 - c)
              * disagreement * weak_scene)
    baseline = 0.45 * exposure + 0.55 * motion

    corridor_floor = 0.50 * p + 0.25 * (a + e)
    return max(0.0, min(1.0, max(baseline, corridor_floor)))
# EVOLVE-BLOCK-END

