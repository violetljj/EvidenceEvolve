# Source: https://github.com/skydiscover-ai/skydiscover
# Commit: 8a840394e19ee4bfb3fb0a62762b902561a7efeb
# Upstream path: benchmarks/math/minimizing_max_min_dist/2/initial_program.py

# EVOLVE-BLOCK-START
import numpy as np


def min_max_dist_dim2_16() -> np.ndarray:
    """Create 16 planar points maximizing minimum/maximum pairwise distance."""
    n = 16
    d = 2
    np.random.seed(42)
    points = np.random.randn(n, d)
    return points


# EVOLVE-BLOCK-END
