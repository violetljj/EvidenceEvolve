from __future__ import annotations


# EVOLVE-BLOCK-START
def compute_risk(f):
    """Blend exposure, reliable motion, and unanimous hazard concurrence."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0,
        0.30 * f["relative_nearness"] + 0.30 * a + 0.25 * e
        + 0.15 * f["path_intrusion"]
        + 0.04 * (f["depth_expansion_consistency"]
                  + f["observation_quality"] - 1.0) * min(a, e)
        + 0.02 * min(f["relative_nearness"], f["path_intrusion"], a, e,
                     f["depth_expansion_consistency"],
                     f["observation_quality"]))
# EVOLVE-BLOCK-END

