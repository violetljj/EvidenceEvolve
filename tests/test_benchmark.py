from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from evidence_evolve.benchmarks.models import ArmTrialSubmission, BenchmarkArm
from evidence_evolve.benchmarks.protocol import (
    BenchmarkProtocolLock,
    load_benchmark_protocol,
)
from evidence_evolve.benchmarks.runner import (
    BenchmarkRunAlreadyActiveError,
    ThreeArmBenchmarkRunner,
    benchmark_run_lock,
)
from tasks.graph_coloring.arm_adapter import scripted_protocol_smoke
from tasks.graph_coloring.evaluator import evaluate_split


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCKED_PROTOCOL = (
    REPO_ROOT / "benchmarks" / "graph_coloring" / "three_arm_v0.locked.yaml"
)


def test_graph_coloring_protocol_is_locked_and_claim_bounded() -> None:
    protocol = load_benchmark_protocol(LOCKED_PROTOCOL)
    report = BenchmarkProtocolLock(REPO_ROOT).validate(protocol)

    assert report.valid
    assert set(protocol.arms) == set(BenchmarkArm)
    assert len(protocol.trial_seeds) == 10
    assert protocol.blind_confirmation_available is False
    assert protocol.claim_scope == "BENCHMARK_PROTOCOL_SMOKE_ONLY"
    assert "PUBLIC_FRESH_IS_NOT_BLIND_CONFIRMATION" in report.warnings


def test_three_arm_scripted_smoke_is_paired_idempotent_and_non_promotional(
    tmp_path: Path,
) -> None:
    protocol = load_benchmark_protocol(LOCKED_PROTOCOL)
    runner = ThreeArmBenchmarkRunner(
        protocol=protocol,
        repo_root=REPO_ROOT,
        run_dir=tmp_path / "run",
        adapter=scripted_protocol_smoke,
    )

    first = runner.run()
    second = runner.run()

    assert first == second
    assert first.decision == "NOT_EVALUABLE_BLIND_CONFIRMATION_UNAVAILABLE"
    assert first.superiority_claim_permitted is False
    assert first.paired_primary_deltas_vs_vanilla == {
        "EVIDENCE_EVOLVE_NO_MEMORY": 0.0,
        "EVIDENCE_EVOLVE_FULL": 0.0,
    }
    assert all(summary.trial_count == 10 for summary in first.arms)
    assert all(summary.public_fresh_valid_rate == 1.0 for summary in first.arms)
    assert len(list((tmp_path / "run" / "trials").rglob("receipt.json"))) == 30


def test_runner_rejects_candidate_budget_overrun(tmp_path: Path) -> None:
    protocol = load_benchmark_protocol(LOCKED_PROTOCOL)

    def excessive_adapter(_context):
        return ArmTrialSubmission(
            executor_id="over-budget",
            candidate_paths=[
                "tasks/graph_coloring/candidates/baseline.py",
                "tasks/graph_coloring/candidates/baseline.py",
            ],
            proposal_calls_used=0,
            token_count_used=0,
        )

    runner = ThreeArmBenchmarkRunner(
        protocol=protocol,
        repo_root=REPO_ROOT,
        run_dir=tmp_path / "run",
        adapter=excessive_adapter,
    )
    with pytest.raises(ValueError, match="candidate evaluation budget exceeded"):
        runner.run()


def test_run_lock_rejects_a_second_process_and_allows_later_resume(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "run" / "run.lock"
    script = (
        "from pathlib import Path; import sys; "
        "from evidence_evolve.benchmarks.runner import benchmark_run_lock; "
        "\nwith benchmark_run_lock(Path(sys.argv[1])):\n    pass\n"
    )
    with benchmark_run_lock(lock_path):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert completed.returncode != 0
    assert "BenchmarkRunAlreadyActiveError" in completed.stderr

    with benchmark_run_lock(lock_path):
        pass


def test_graph_coloring_evaluator_fails_closed_on_invalid_candidate(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "invalid_candidate.py"
    candidate.write_text(
        "def solve(node_count, edges, seed):\n"
        "    return [0] * node_count\n",
        encoding="utf-8",
    )
    protocol = load_benchmark_protocol(LOCKED_PROTOCOL)

    result = evaluate_split(
        candidate,
        protocol.development.instances,
        visibility=protocol.development.visibility,
        trial_seed=protocol.trial_seeds[0],
    )

    assert result.valid_rate == 0.0
    assert result.reproducibility_rate == 0.0
    assert result.positive_relative_improvement == 0.0
    assert all("EDGE_CONFLICT" in reason for reason in result.failure_reasons)
