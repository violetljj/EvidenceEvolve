# Source: https://github.com/skydiscover-ai/skydiscover
# Commit: 8a840394e19ee4bfb3fb0a62762b902561a7efeb
# Upstream path: benchmarks/math/minimizing_max_min_dist/2/initial_program.py

# EVOLVE-BLOCK-START
import numpy as np
def min_max_dist_dim2_16() -> np.ndarray:
    """Return a deterministic optimized 16-point planar configuration."""
    return np.asarray(
        [
            [-1.479755610000,  0.980983570000],
            [ 0.851848075446,  0.072110384570],
            [-0.734611602961,  1.647887168108],
            [ 0.151273184884, -1.789755757508],
            [-0.715592895553,  0.335960046209],
            [-1.545477112098, -0.918920851705],
            [ 1.733002311024, -0.451602182759],
            [-1.823092802796,  0.041771361583],
            [ 1.731138662180,  0.548396080844],
            [ 1.155331899164,  1.365981900229],
            [ 0.404917250668, -0.822458136148],
            [-0.580950766088, -0.654934245514],
            [ 0.168877531232,  0.802556300522],
            [ 0.253914996742,  1.798934055119],
            [-0.834594831872, -1.622231866873],
            [ 1.263771700000, -1.334677850000],
        ],
        dtype=np.float64,
    )
# EVOLVE-BLOCK-END
