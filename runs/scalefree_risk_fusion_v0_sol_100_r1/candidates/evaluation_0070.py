# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse corridor exposure with confidence-gated motion and a corroboration floor."""
    n = min(1.0, max(0.0, f["relative_nearness"]))
    a = min(1.0, max(0.0, f["depth_approach_rate"]))
    e = min(1.0, max(0.0, f["local_expansion"]))
    p = min(1.0, max(0.0, f["path_intrusion"]))
    c = min(1.0, max(0.0, f["depth_expansion_consistency"]))
    q = min(1.0, max(0.0, f["observation_quality"]))

    corroborated = min(a, e)
    motion = 0.52 * a + 0.38 * e + 0.10 * corroborated
    reliability = 0.65 + 0.20 * c + 0.15 * q

    # Intrusion amplifies nearness and reliable motion without erasing
    # off-corridor evidence entirely.
    exposure = n * (0.72 + 0.28 * p)
    dynamic = motion * reliability * (0.78 + 0.22 * p)
    base = 0.34 * exposure + 0.44 * dynamic + 0.14 * p + 0.08 * n

    # Agreement between independent motion cues prevents dangerous clears.
    rescue = (0.25 * n + 0.18 * p + 0.57 * corroborated) * (0.82 + 0.18 * c)
    risk = max(base, rescue)
    return min(1.0, max(0.0, risk))
# EVOLVE-BLOCK-END

