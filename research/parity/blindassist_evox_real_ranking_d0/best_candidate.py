"""Frozen unoptimized D0 hand-written fusion baseline."""

from __future__ import annotations

import math


# EVOLVE-BLOCK-START
def compute_risk(features):
    """Rank hazards by balanced nearness and path intrusion."""
    return min(1.0, max(0.0, 0.36 * features["relative_nearness"] + 0.34 * features["path_intrusion"]))
# EVOLVE-BLOCK-END
