from __future__ import annotations

from typing import Protocol

from evidence_evolve.search.models import SearchRunReceipt, SearchRunRequest


class SearchEngine(Protocol):
    """Thin interface for upstream or experimental search implementations."""

    def run(self, request: SearchRunRequest) -> SearchRunReceipt: ...
