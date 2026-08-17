from __future__ import annotations

from evidence_evolve.benchmarks.engine_selection_shinka_postfix_runner import (
    REPEATS,
    TASKS,
    load_protocol,
)


def test_postfix_protocol_is_six_short_shinka_only_development_runs() -> None:
    protocol = load_protocol()

    assert TASKS == ("pde_heat1d", "convex_hull", "communicability")
    assert REPEATS == (1, 2)
    assert protocol["confirmation"]["arms"] == ["shinka"]
    assert protocol["confirmation"]["native_iterations"] == 12
    assert protocol["confirmation"]["run_count"] == 6
    assert protocol["heldout"]["enabled"] is False
    assert protocol["superiority_claim_permitted"] is False
    assert all(protocol["mechanics_gates"].values())
