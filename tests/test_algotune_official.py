from pathlib import Path

from evidence_evolve.benchmarks.algotune_official import (
    OfficialTaskSpec,
    evaluate_official_candidate,
    evaluate_official_candidate_cold,
)


def test_official_adapter_compares_candidate_to_immutable_oracle(tmp_path: Path) -> None:
    source = tmp_path / "task.py"
    source.write_text(
        "from AlgoTuneTasks.base import Task, register_task\n"
        "@register_task('toy')\n"
        "class Toy(Task):\n"
        "    def generate_problem(self, n, random_seed=1): return n + random_seed\n"
        "    def solve(self, problem): return problem * 2\n"
        "    def is_solution(self, problem, solution): return solution == problem * 2\n",
        encoding="utf-8",
    )
    result = evaluate_official_candidate(
        source,
        OfficialTaskSpec("toy", "Toy", 5, str(source)),
        [1, 2, 3],
        repeats=2,
        workers=2,
    )
    assert result["correct"] is True
    assert result["valid_rate"] == 1.0
    assert result["raw_speedup"] > 0.0


def test_official_adapter_fails_closed_on_wrong_result(tmp_path: Path) -> None:
    oracle = tmp_path / "oracle.py"
    candidate = tmp_path / "candidate.py"
    shared = (
        "from AlgoTuneTasks.base import Task, register_task\n"
        "@register_task('toy')\n"
        "class Toy(Task):\n"
        "    def generate_problem(self, n, random_seed=1): return n + random_seed\n"
    )
    oracle.write_text(
        shared
        + "    def solve(self, problem): return problem * 2\n"
        + "    def is_solution(self, problem, solution): return solution == problem * 2\n",
        encoding="utf-8",
    )
    candidate.write_text(
        shared + "    def solve(self, problem): return -1\n",
        encoding="utf-8",
    )
    result = evaluate_official_candidate(
        candidate,
        OfficialTaskSpec("toy", "Toy", 5, str(oracle)),
        [1],
        repeats=1,
        workers=1,
    )
    assert result["correct"] is False
    assert result["valid_rate"] == 0.0
    assert result["raw_speedup"] == 0.0


def test_cold_adapter_uses_one_fresh_process_per_repeat(tmp_path: Path) -> None:
    source = tmp_path / "task.py"
    source.write_text(
        "from AlgoTuneTasks.base import Task, register_task\n"
        "@register_task('toy')\n"
        "class Toy(Task):\n"
        "    def generate_problem(self, n, random_seed=1): return n + random_seed\n"
        "    def solve(self, problem): return problem * 2\n"
        "    def is_solution(self, problem, solution): return solution == problem * 2\n",
        encoding="utf-8",
    )
    result = evaluate_official_candidate_cold(
        source,
        OfficialTaskSpec("toy", "Toy", 5, str(source)),
        [1, 2],
        repeats=3,
        workers=2,
    )
    assert result["correct"] is True
    assert result["trial_count"] == 6
    assert result["status_counts"] == {"PASS": 6}
    assert result["fresh_process_per_solver_call"] is True


def test_cold_adapter_attributes_candidate_timeout(tmp_path: Path) -> None:
    oracle = tmp_path / "oracle.py"
    candidate = tmp_path / "candidate.py"
    shared = (
        "from AlgoTuneTasks.base import Task, register_task\n"
        "@register_task('toy')\n"
        "class Toy(Task):\n"
        "    def generate_problem(self, n, random_seed=1): return n + random_seed\n"
    )
    oracle.write_text(
        shared
        + "    def solve(self, problem): return problem * 2\n"
        + "    def is_solution(self, problem, solution): return solution == problem * 2\n",
        encoding="utf-8",
    )
    candidate.write_text(
        "import time\n"
        + shared
        + "    def solve(self, problem): time.sleep(1); return problem * 2\n",
        encoding="utf-8",
    )
    result = evaluate_official_candidate_cold(
        candidate,
        OfficialTaskSpec("toy", "Toy", 5, str(oracle)),
        [1],
        repeats=1,
        workers=1,
        timeout_seconds=0.2,
    )
    assert result["correct"] is False
    assert result["status_counts"] == {"TIMEOUT_CANDIDATE": 1}
