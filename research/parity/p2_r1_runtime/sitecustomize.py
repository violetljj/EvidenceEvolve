"""Frozen P2-R1 process bootstrap: paired seed plus transport audit."""

from __future__ import annotations

import os
import runpy


seed_layer = os.environ.get("EVIDENCE_EVOLVE_P2_R1_SEED_LAYER")
if not seed_layer:
    raise RuntimeError("EVIDENCE_EVOLVE_P2_R1_SEED_LAYER is required")
runpy.run_path(seed_layer, run_name="evidence_evolve_p2_r1_seed_layer")

from evidence_evolve.proposals.p2_r1_transport import (  # noqa: E402
    install_transport_audit_from_environment,
)


install_transport_audit_from_environment()
