from evidence_evolve.benchmarks.algotune_falsification import (
    _aggregate,
    _candidate_solve_count,
    generate_density_problem,
)


def test_density_generator_is_deterministic_and_covers_universe() -> None:
    first = generate_density_problem(40, 123, 0.08)
    second = generate_density_problem(40, 123, 0.08)
    assert first == second
    assert {item for subset in first for item in subset} == set(range(1, 41))


def test_cold_aggregation_requires_every_repeat_to_pass() -> None:
    rows = [
        {
            "arm": "evidence_evolve",
            "size": 52,
            "density": None,
            "seed": 123,
            "repeat": 0,
            "status": "PASS",
            "candidate_ns": 10,
            "reference_ns": 100,
        },
        {
            "arm": "evidence_evolve",
            "size": 52,
            "density": None,
            "seed": 123,
            "repeat": 1,
            "status": "CANDIDATE_TIMEOUT",
            "reference_ns": 100,
        },
    ]
    summary = _aggregate(rows)[0]
    assert summary["correct"] is False
    assert summary["valid_rate"] == 0.5
    assert summary["raw_speedup"] == 0.0


def test_reference_timeout_is_not_counted_as_candidate_execution() -> None:
    rows = [
        {
            "arm": "evidence_evolve",
            "size": 100,
            "density": 0.08,
            "seed": 123,
            "repeat": 0,
            "status": "REFERENCE_TIMEOUT",
        }
    ]
    summary = _aggregate(rows)[0]
    assert summary["correct"] is False
    assert summary["status_counts"] == {"REFERENCE_TIMEOUT": 1}
    assert summary["raw_speedup"] == 0.0
    assert _candidate_solve_count(rows) == 0


def test_candidate_timeouts_and_invalid_results_count_as_executions() -> None:
    rows = [
        {"status": "PASS"},
        {"status": "CANDIDATE_TIMEOUT"},
        {"status": "INVALID_SOLUTION"},
        {"status": "REFERENCE_TIMEOUT"},
        {"status": "INSTANCE_PROCESS_ERROR"},
    ]
    assert _candidate_solve_count(rows) == 3
