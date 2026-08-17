from __future__ import annotations

from typing import Any


def shinka_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Expose development speedup in the schema Shinka uses for selection."""
    correct = bool(result["correct"])
    raw_speedup = float(result["raw_speedup"])
    valid_rate = float(result["valid_rate"])
    return {
        **result,
        "combined_score": raw_speedup if correct else 0.0,
        "public": {"raw_speedup": raw_speedup, "valid_rate": valid_rate},
        "private": {},
    }
