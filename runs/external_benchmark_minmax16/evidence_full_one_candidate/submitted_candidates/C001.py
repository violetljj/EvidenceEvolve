# Source: https://github.com/skydiscover-ai/skydiscover
# Commit: 8a840394e19ee4bfb3fb0a62762b902561a7efeb
# Upstream path: benchmarks/math/minimizing_max_min_dist/2/initial_program.py

# EVOLVE-BLOCK-START
import numpy as np


def min_max_dist_dim2_16() -> np.ndarray:
    """Create 16 planar points maximizing minimum/maximum pairwise distance."""
    horizontal_spacing = 1.0
    vertical_spacing = np.sqrt(3.0) / 2.0
    points = np.array(
        [
            (column * horizontal_spacing + (row % 2) * horizontal_spacing / 2.0,
             row * vertical_spacing)
            for row in range(4)
            for column in range(4)
        ],
        dtype=float,
    )

    points -= points.mean(axis=0)
    pairwise_offsets = points[:, np.newaxis, :] - points[np.newaxis, :, :]
    diameter = np.sqrt(np.max(np.sum(pairwise_offsets**2, axis=-1)))
    return points / diameter


# EVOLVE-BLOCK-END
