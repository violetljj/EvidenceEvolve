# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure and motion with a safety rescue and recession veto."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))
    da = min(1.0, max(-1.0, f["depth_approach_rate"]))
    le = min(1.0, max(-1.0, f["local_expansion"]))
    a, e = max(0.0, da), max(0.0, le)

    exposure = 0.65 * n + 0.35 * p
    motion = 0.55 * a + 0.45 * e
    agreement = min(a, e)
    corridor_rescue = p * (0.5 * max(a, e) + 0.5 * agreement)
    noisy_conflict = (1.0 - q) * abs(a - e)
    recession = min(max(0.0, -da), max(0.0, -le))

    risk = (0.45 * exposure + 0.55 * motion
            + 0.045 * (0.5 + 0.5 * c) * corridor_rescue
            + 0.025 * (c + q - 1.0) * min(exposure, motion)
            - 0.025 * noisy_conflict
            - 0.035 * q * recession)
    return min(1.0, max(0.0, risk))
# EVOLVE-BLOCK-END

