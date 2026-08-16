# EVOLVE-BLOCK-START
def compute_risk(f):
    """Take the safer of general evidence and a corroborated corridor regime."""
    def unit(x):
        return min(1.0, max(0.0, x))

    n = unit(f["relative_nearness"])
    a = unit(f["depth_approach_rate"])
    e = unit(f["local_expansion"])
    p = unit(f["path_intrusion"])
    c = unit(f["depth_expansion_consistency"])
    q = unit(f["observation_quality"])

    lo = min(a, e)
    hi = max(a, e)
    motion = lo + (0.52 + 0.16 * c + 0.08 * q) * (hi - lo)

    general = 0.31 * n + 0.12 * p + 0.49 * motion \
              + 0.08 * q * lo
    corridor = 0.22 * n + 0.33 * p \
               + 0.45 * (lo + c * (hi - lo))
    return min(1.0, max(general, corridor))
# EVOLVE-BLOCK-END

