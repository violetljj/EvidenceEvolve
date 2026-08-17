from __future__ import annotations

import copy

import pytest

from evidence_evolve.benchmarks.engine_selection_r3_runner import (
    ARMS,
    load_protocol,
    validate_protocol,
)


def test_r3_protocol_is_visible_dev_only_and_token_never_stops() -> None:
    protocol = load_protocol()

    assert tuple(protocol["arms"]) == ARMS
    assert protocol["benchmark_policy"]["fresh_tasks_spent"] == 0
    assert protocol["benchmark_policy"]["development_results_visible"] is True
    assert protocol["common_conditions"]["token_hard_ceiling"] is None
    assert protocol["common_conditions"]["token_call_launch_ceiling"] is None
    assert protocol["heldout"]["enabled"] is False
    assert protocol["ranking"]["formal_winner_permitted"] is False


def test_r3_rejects_token_stop_or_premature_winner() -> None:
    protocol = load_protocol()
    token_limited = copy.deepcopy(protocol)
    token_limited["common_conditions"]["token_hard_ceiling"] = 1
    with pytest.raises(ValueError, match="token stop"):
        validate_protocol(token_limited)

    winner = copy.deepcopy(protocol)
    winner["ranking"]["formal_winner_permitted"] = True
    with pytest.raises(ValueError, match="cannot claim a winner"):
        validate_protocol(winner)
