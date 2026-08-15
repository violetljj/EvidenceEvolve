from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from importlib import metadata


@dataclass(frozen=True)
class ShinkaStatus:
    installed: bool
    version: str | None = None
    expected_version: str = "0.0.7"
    distribution: str = "shinka-evolve"
    import_name: str = "shinka"
    source_commit: str | None = None
    expected_source_commit: str = "c4568adde253cacf185be3a8412c3c2142761ebe"
    source_commit_verified: bool = False
    verdict_authority: str = "EVIDENCE_EVOLVE_ONLY"
    combined_score_usage: str = "SCHEDULING_ONLY"


class ShinkaBackend:
    """Optional search-kernel seam; never a verdict authority."""

    def status(self) -> ShinkaStatus:
        installed = importlib.util.find_spec("shinka") is not None
        version: str | None = None
        source_commit: str | None = None
        if installed:
            try:
                distribution = metadata.distribution("shinka-evolve")
                version = distribution.version
                direct_url = distribution.read_text("direct_url.json")
                if direct_url:
                    payload = json.loads(direct_url)
                    vcs_info = payload.get("vcs_info", {})
                    if isinstance(vcs_info, dict):
                        value = vcs_info.get("commit_id")
                        if isinstance(value, str):
                            source_commit = value
            except (metadata.PackageNotFoundError, json.JSONDecodeError):
                pass
        expected = "c4568adde253cacf185be3a8412c3c2142761ebe"
        return ShinkaStatus(
            installed=installed,
            version=version,
            source_commit=source_commit,
            source_commit_verified=source_commit == expected,
        )

    def require(self) -> None:
        status = self.status()
        if not status.installed:
            raise RuntimeError(
                "ShinkaEvolve is not installed. Install the optional 'shinka' extra "
                "before running native evolutionary search."
            )
        if status.version != status.expected_version:
            raise RuntimeError(
                "ShinkaEvolve version mismatch: "
                f"expected={status.expected_version} actual={status.version}"
            )
        if not status.source_commit_verified:
            raise RuntimeError(
                "ShinkaEvolve source commit is not verified: "
                f"expected={status.expected_source_commit} actual={status.source_commit}"
            )
