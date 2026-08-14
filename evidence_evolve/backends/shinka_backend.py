from __future__ import annotations

import importlib.util
from dataclasses import dataclass


@dataclass(frozen=True)
class ShinkaStatus:
    installed: bool
    distribution: str = "shinka-evolve"
    import_name: str = "shinka"
    verdict_authority: str = "EVIDENCE_EVOLVE_ONLY"
    combined_score_usage: str = "SCHEDULING_ONLY"


class ShinkaBackend:
    """Optional search-kernel seam; never a verdict authority."""

    def status(self) -> ShinkaStatus:
        return ShinkaStatus(installed=importlib.util.find_spec("shinka") is not None)

    def require(self) -> None:
        status = self.status()
        if not status.installed:
            raise RuntimeError(
                "ShinkaEvolve is not installed. Install the optional 'shinka' extra "
                "in a Python 3.11 environment before running evolutionary search."
            )

