from __future__ import annotations

import copy

import pytest

from evidence_evolve.benchmarks.engine_selection_r3_continuation_runner import load_protocol


def test_continuation_binds_all_parent_states_and_has_no_token_stop() -> None:
    protocol = load_protocol()

    assert protocol["continuation"]["additional_native_iterations"] == 30
    assert set(protocol["parent_bindings"]) == {"pde_heat1d", "convex_hull", "communicability"}
    assert all(set(bindings) == {"vanilla", "ada", "shinka", "evox"} for bindings in protocol["parent_bindings"].values())
    assert protocol["common_conditions"]["token_hard_ceiling"] is None
    assert protocol["common_conditions"]["token_call_launch_ceiling"] is None
    assert protocol["heldout"]["enabled"] is False
