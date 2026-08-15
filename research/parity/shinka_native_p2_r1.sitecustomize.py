"""Frozen process-local seed layer for SHINKA_NATIVE_P2_R1."""

from __future__ import annotations

import os
import random

import numpy as np


ALLOWED_SEEDS = frozenset(range(2026081501, 2026081511))
SEED_ENV = "EVIDENCE_EVOLVE_P2_R1_SEED"

raw_seed = os.environ.get(SEED_ENV)
if raw_seed is None:
    raise RuntimeError(f"{SEED_ENV} is required by the frozen P2-R1 seed layer")

try:
    seed = int(raw_seed)
except ValueError as error:
    raise RuntimeError(f"{SEED_ENV} must be an integer") from error

if seed not in ALLOWED_SEEDS:
    raise RuntimeError(f"{SEED_ENV} is not a frozen P2-R1 seed: {seed}")

random.seed(seed)
np.random.seed(seed)
