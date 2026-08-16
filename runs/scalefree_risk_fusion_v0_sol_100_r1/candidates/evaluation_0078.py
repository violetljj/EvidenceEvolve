# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and corridor-gated motion with a near-intrusion safety floor."""
    n = f["relative_nearness"]
    p = f["path_intrusion"]
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    agreement = min(a, e)
    reliability = 0.5 * (f["depth_expansion_consistency"]
                         + f["observation_quality"])

    exposure = 0.29 * n + 0.14 * p + 0.07 * n * p
    motion = (0.31 * a + 0.23 * e) * (0.78 + 0.22 * p)
    support = 0.08 * agreement * (0.5 + 0.5 * reliability)
    safety_floor = 0.58 * n * p + 0.20 * agreement * reliability

    return min(1.0, max(0.0, max(exposure + motion + support,
                                  safety_floor)))
# EVOLVE-BLOCK-END

