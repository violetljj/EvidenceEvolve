# EVOLVE-BLOCK-START
def compute_risk(f):
    """Fuse exposure with positive approach and looming evidence monotonically."""
    a = max(0.0, f["depth_approach_rate"])
    e = max(0.0, f["local_expansion"])
    return min(1.0, 0.30 * f["relative_nearness"] + 0.30 * a
               + 0.25 * e + 0.15 * f["path_intrusion"])
# EVOLVE-BLOCK-END

