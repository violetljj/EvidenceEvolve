from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

upstream_cli = pytest.importorskip("shinka.cli.run")

from evidence_evolve.search.models import SearchRunRequest
from evidence_evolve.search.shinka_native import ShinkaNativeEngine
from evidence_evolve.proposals.models import ProposalMaterializerMode
from evidence_evolve.proposals.shinka_adapter import (
    apply_evidence_diff_patch,
    run_official_shinka_with_materializer,
)


def _make_task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    task.mkdir()
    (task / "initial.py").write_text(
        "# EVOLVE-BLOCK-START\ndef solve():\n    return 0\n# EVOLVE-BLOCK-END\n",
        encoding="utf-8",
    )
    (task / "evaluate.py").write_text(
        "def main(program_path, results_dir):\n    return None\n",
        encoding="utf-8",
    )
    (task / "shinka.yaml").write_text(
        "max_evaluation_jobs: 3\n"
        "max_proposal_jobs: 4\n"
        "max_db_workers: 2\n"
        "verbose: false\n"
        "db_config:\n"
        "  num_islands: 3\n"
        "  parent_selection_strategy: power_law\n"
        "job_config:\n"
        "  time: 00:02:00\n"
        "evo_config:\n"
        "  num_generations: 999\n"
        "  results_dir: ignored-by-upstream-cli\n"
        "  patch_types: [diff, full, cross]\n"
        "  patch_type_probs: [0.5, 0.3, 0.2]\n"
        "  llm_models: [fake/model]\n"
        "  embedding_model: null\n",
        encoding="utf-8",
    )
    return task


def _write_native_database(results_dir: Path) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    database = results_dir / "programs.sqlite"
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            "CREATE TABLE programs ("
            "id TEXT PRIMARY KEY, parent_id TEXT, archive_inspiration_ids TEXT, "
            "top_k_inspiration_ids TEXT, generation INTEGER, combined_score REAL, "
            "public_metrics TEXT, private_metrics TEXT, correct BOOLEAN, metadata TEXT);"
            "CREATE TABLE archive (program_id TEXT PRIMARY KEY);"
            "CREATE TABLE attempt_log (id INTEGER PRIMARY KEY);"
            "CREATE TABLE generation_event_log (id INTEGER PRIMARY KEY);"
        )
        connection.execute(
            "INSERT INTO programs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "seed",
                None,
                "[]",
                "[]",
                0,
                1.0,
                json.dumps({"score": 1.0}),
                "{}",
                1,
                json.dumps({"api_costs": 0.0}),
            ),
        )
        connection.execute(
            "INSERT INTO programs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "candidate-1",
                "seed",
                json.dumps(["seed"]),
                "[]",
                1,
                1.25,
                json.dumps({"score": 1.25}),
                "{}",
                1,
                json.dumps(
                    {
                        "api_costs": 0.01,
                        "llm_result": {"input_tokens": 100, "output_tokens": 25},
                    }
                ),
            ),
        )
        connection.execute("INSERT INTO archive VALUES ('candidate-1')")
        connection.execute("INSERT INTO attempt_log VALUES (1)")
        connection.execute("INSERT INTO generation_event_log VALUES (1)")
        connection.commit()
    finally:
        connection.close()


class _CapturingRunner:
    captures: list[dict] = []
    events: list[list[dict]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.captures.append(kwargs)

    def run(self):
        evo = self.kwargs["evo_config"]
        db = self.kwargs["db_config"]
        event_stream = [
            {"candidate": "seed", "parent": None, "metric": 1.0},
            {
                "candidate": "candidate-1",
                "parent": "seed",
                "metric": 1.25,
                "islands": db.num_islands,
                "patch_types": list(evo.patch_types),
            },
        ]
        self.__class__.events.append(event_stream)
        _write_native_database(Path(evo.results_dir))


class _MaterializerObservingRunner(_CapturingRunner):
    observed_materializer = None

    def run(self):
        import shinka.edit.async_apply as async_apply

        self.__class__.observed_materializer = async_apply.apply_diff_patch
        super().run()


def _normalized_kwargs(kwargs: dict) -> dict:
    return {
        "evo": {
            key: value
            for key, value in asdict(kwargs["evo_config"]).items()
            if key != "results_dir"
        },
        "db": asdict(kwargs["db_config"]),
        "job": asdict(kwargs["job_config"]),
        "runner": {
            key: value
            for key, value in kwargs.items()
            if key not in {"evo_config", "db_config", "job_config"}
        },
    }


def test_shinka_native_matches_upstream_cli_construction_and_import_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    task = _make_task(tmp_path)
    direct_results = tmp_path / "direct"
    engine_results = tmp_path / "engine"
    _CapturingRunner.captures = []
    _CapturingRunner.events = []
    monkeypatch.setattr(upstream_cli, "ShinkaEvolveRunner", _CapturingRunner)

    assert (
        upstream_cli.main(
            [
                "--task-dir",
                str(task),
                "--config-fname",
                "shinka.yaml",
                "--results_dir",
                str(direct_results),
                "--num_generations",
                "5",
                "--set",
                "db.migration_rate=0.2",
            ]
        )
        == 0
    )
    direct_kwargs = _CapturingRunner.captures[-1]
    direct_events = _CapturingRunner.events[-1]

    request = SearchRunRequest(
        run_id="parity-001",
        task_dir=task,
        results_dir=engine_results,
        num_generations=5,
        config_fname="shinka.yaml",
        set_overrides=["db.migration_rate=0.2"],
    )
    receipt = ShinkaNativeEngine(runner_factory=_CapturingRunner).run(request)
    engine_kwargs = _CapturingRunner.captures[-1]
    engine_events = _CapturingRunner.events[-1]

    assert _normalized_kwargs(engine_kwargs) == _normalized_kwargs(direct_kwargs)
    assert engine_events == direct_events
    assert receipt.imported.best_program_id == "candidate-1"
    assert receipt.imported.best_combined_score == 1.25
    assert receipt.imported.input_tokens_observed == 100
    assert receipt.imported.output_tokens_observed == 25
    assert receipt.scientific_outcome_authority == "NONE"
    assert receipt.superiority_claim_permitted is False
    assert (engine_results / "programs.sqlite").is_file()
    assert (
        engine_results / "evidence_evolve" / "receipts" / "parity-001.json"
    ).is_file()


def test_shinka_native_passes_all_current_upstream_namespaces_without_replacing_search(
    tmp_path: Path,
) -> None:
    task = _make_task(tmp_path)
    request = SearchRunRequest(
        run_id="config-001",
        task_dir=task,
        results_dir=tmp_path / "results",
        num_generations=7,
        config_fname="shinka.yaml",
        set_overrides=[
            "evo.max_novelty_attempts=5",
            "db.archive_size=77",
            'job.extra_cmd_args={"seed":42}',
        ],
        max_proposal_jobs=9,
        verbose=True,
    )
    kwargs, _effective = ShinkaNativeEngine()._runner_kwargs(request)

    assert kwargs["evo_config"].num_generations == 7
    assert kwargs["evo_config"].patch_types == ["diff", "full", "cross"]
    assert kwargs["evo_config"].max_novelty_attempts == 5
    assert kwargs["db_config"].num_islands == 3
    assert kwargs["db_config"].archive_size == 77
    assert kwargs["db_config"].parent_selection_strategy == "power_law"
    assert kwargs["job_config"].time == "00:02:00"
    assert kwargs["job_config"].extra_cmd_args == {"seed": 42}
    assert kwargs["max_evaluation_jobs"] == 3
    assert kwargs["max_proposal_jobs"] == 9
    assert kwargs["max_db_workers"] == 2
    assert kwargs["verbose"] is True


def test_shinka_native_installs_shared_materializer_only_for_the_run(
    tmp_path: Path,
) -> None:
    import shinka.edit.async_apply as async_apply

    task = _make_task(tmp_path)
    original = async_apply.apply_diff_patch
    request = SearchRunRequest(
        run_id="materialized-001",
        task_dir=task,
        results_dir=tmp_path / "results",
        num_generations=1,
        config_fname="shinka.yaml",
        proposal_materializer=ProposalMaterializerMode.EVIDENCE_EVOLVE_V1,
    )

    receipt = ShinkaNativeEngine(
        runner_factory=_MaterializerObservingRunner
    ).run(request)

    assert _MaterializerObservingRunner.observed_materializer is apply_evidence_diff_patch
    assert async_apply.apply_diff_patch is original
    assert receipt.proposal_materializer == ProposalMaterializerMode.EVIDENCE_EVOLVE_V1
    assert (
        receipt.claim_scope
        == "UPSTREAM_SEARCH_WITH_EVIDENCE_EVOLVE_MATERIALIZATION_AND_IMPORT"
    )


def test_official_shinka_entrypoint_installs_the_same_shared_materializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shinka.edit.async_apply as async_apply

    original = async_apply.apply_diff_patch
    observed = []

    def fake_main(arguments: list[str]) -> int:
        observed.append((arguments, async_apply.apply_diff_patch))
        return 0

    monkeypatch.setattr(upstream_cli, "main", fake_main)

    assert run_official_shinka_with_materializer(["--help"]) == 0
    assert observed == [(["--help"], apply_evidence_diff_patch)]
    assert async_apply.apply_diff_patch is original
