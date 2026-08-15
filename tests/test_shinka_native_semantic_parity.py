from __future__ import annotations

import json
import random
import re
import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any

import numpy as np
import pytest

upstream_cli = pytest.importorskip("shinka.cli.run")
async_runner = pytest.importorskip("shinka.core.async_runner")

from shinka.llm.providers import QueryResult
from rich.console import Console as RichConsole

from evidence_evolve.search.models import SearchRunRequest
from evidence_evolve.search.shinka_native import ShinkaNativeEngine


SEED = 1729


class _DeterministicAsyncLLMClient:
    """Provider-boundary fake; all search/database/evaluator code remains upstream."""

    def __init__(self, model_names: list[str] | str, **_kwargs: Any) -> None:
        self.model_names = (
            list(model_names) if isinstance(model_names, list) else [model_names]
        )

    def get_kwargs(self, model_sample_probs: list[float] | None = None) -> dict:
        del model_sample_probs
        return {
            "model_name": self.model_names[0],
            "temperature": 0.0,
            "max_tokens": 1024,
        }

    async def query(
        self,
        msg: str,
        system_msg: str,
        msg_history: list[dict] | None = None,
        llm_kwargs: dict | None = None,
        model_sample_probs: list[float] | None = None,
        model_posterior: list[float] | None = None,
    ) -> QueryResult:
        del system_msg, model_sample_probs, model_posterior
        matches = re.findall(r"VALUE\s*=\s*(\d+)", msg)
        parent_value = int(matches[-1]) if matches else 0
        value = parent_value + 1
        content = (
            "<NAME>deterministic_increment</NAME>\n"
            "<DESCRIPTION>Increment the controlled score.</DESCRIPTION>\n"
            "<CODE>\n```python\n"
            "# EVOLVE-BLOCK-START\n"
            f"VALUE = {value}\n"
            "\n"
            "def solve():\n"
            "    return VALUE\n"
            "# EVOLVE-BLOCK-END\n"
            "```\n</CODE>"
        )
        kwargs = llm_kwargs or self.get_kwargs()
        return QueryResult(
            content=content,
            msg=msg,
            system_msg="deterministic-provider",
            new_msg_history=[*(msg_history or []), {"role": "assistant", "content": content}],
            model_name=self.model_names[0],
            kwargs=kwargs,
            input_tokens=11,
            output_tokens=7,
            cost=0.0,
        )


def _make_task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    task.mkdir()
    (task / "initial.py").write_text(
        "# EVOLVE-BLOCK-START\n"
        "VALUE = 0\n\n"
        "def solve():\n"
        "    return VALUE\n"
        "# EVOLVE-BLOCK-END\n",
        encoding="utf-8",
    )
    (task / "evaluate.py").write_text(
        "import argparse\n"
        "import importlib.util\n"
        "import json\n"
        "from pathlib import Path\n\n"
        "def main(program_path: str, results_dir: str) -> None:\n"
        "    spec = importlib.util.spec_from_file_location('candidate', program_path)\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    assert spec.loader is not None\n"
        "    spec.loader.exec_module(module)\n"
        "    score = float(module.solve())\n"
        "    output = Path(results_dir)\n"
        "    output.mkdir(parents=True, exist_ok=True)\n"
        "    (output / 'metrics.json').write_text(json.dumps({\n"
        "        'combined_score': score,\n"
        "        'public': {'score': score},\n"
        "        'private': {'controlled': True}\n"
        "    }), encoding='utf-8')\n"
        "    (output / 'correct.json').write_text(json.dumps({\n"
        "        'correct': True, 'error': ''\n"
        "    }), encoding='utf-8')\n\n"
        "if __name__ == '__main__':\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--program_path', required=True)\n"
        "    parser.add_argument('--results_dir', required=True)\n"
        "    args = parser.parse_args()\n"
        "    main(args.program_path, args.results_dir)\n",
        encoding="utf-8",
    )
    (task / "shinka.yaml").write_text(
        "max_evaluation_jobs: 1\n"
        "max_proposal_jobs: 1\n"
        "max_db_workers: 1\n"
        "verbose: false\n"
        "evo_config:\n"
        "  llm_models: [headless/deterministic-parity]\n"
        "  llm_dynamic_selection: null\n"
        "  llm_kwargs:\n"
        "    temperatures: [0.0]\n"
        "    max_tokens: 1024\n"
        "  patch_types: [full]\n"
        "  patch_type_probs: [1.0]\n"
        "  max_patch_resamples: 1\n"
        "  max_patch_attempts: 1\n"
        "  max_novelty_attempts: 1\n"
        "  embedding_model: null\n"
        "  novelty_llm_models: null\n"
        "  meta_rec_interval: null\n"
        "  evolve_prompts: false\n"
        "  proposal_target_mode: fixed\n"
        "db_config:\n"
        "  num_islands: 2\n"
        "  archive_size: 3\n"
        "  num_archive_inspirations: 0\n"
        "  num_top_k_inspirations: 1\n"
        "  parent_selection_strategy: weighted\n"
        "  island_selection_strategy: equal\n"
        "  migration_interval: 2\n"
        "  migration_rate: 1.0\n"
        "  island_elitism: false\n"
        "  enforce_island_separation: false\n",
        encoding="utf-8",
    )
    return task


def _reset_random_state() -> None:
    random.seed(SEED)
    np.random.seed(SEED)


def _canonical_snapshot(results_dir: Path) -> dict[str, Any]:
    connection = sqlite3.connect(results_dir / "programs.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT rowid, id, parent_id, archive_inspiration_ids, "
            "top_k_inspiration_ids, generation, code, combined_score, "
            "public_metrics, private_metrics, correct, island_idx, "
            "migration_history FROM programs ORDER BY generation, rowid"
        ).fetchall()
        canonical_ids = {row["id"]: f"candidate-{index}" for index, row in enumerate(rows)}

        def ids(value: str | None) -> list[str]:
            return [canonical_ids[item] for item in json.loads(value or "[]")]

        def migrations(value: str | None) -> list[dict[str, Any]]:
            return [
                {key: item[key] for key in ("generation", "from", "to")}
                for item in json.loads(value or "[]")
            ]

        programs = [
            {
                "id": canonical_ids[row["id"]],
                "parent": canonical_ids.get(row["parent_id"]),
                "archive_inspirations": ids(row["archive_inspiration_ids"]),
                "top_k_inspirations": ids(row["top_k_inspiration_ids"]),
                "generation": row["generation"],
                "value": int(re.findall(r"VALUE\s*=\s*(\d+)", row["code"])[-1]),
                "combined_score": row["combined_score"],
                "public_metrics": json.loads(row["public_metrics"] or "{}"),
                "private_metrics": json.loads(row["private_metrics"] or "{}"),
                "correct": bool(row["correct"]),
                "island": row["island_idx"],
                "migration_history": migrations(row["migration_history"]),
            }
            for row in rows
        ]
        archive = [
            canonical_ids[row[0]]
            for row in connection.execute(
                "SELECT program_id FROM archive ORDER BY program_id"
            ).fetchall()
        ]
        generation_events = [
            (row["generation"], row["status"])
            for row in connection.execute(
                "SELECT generation, status FROM generation_event_log ORDER BY id"
            ).fetchall()
        ]
        attempts = [
            (row["generation"], row["stage"], row["status"])
            for row in connection.execute(
                "SELECT generation, stage, status FROM attempt_log ORDER BY id"
            ).fetchall()
        ]
        metadata = dict(
            connection.execute("SELECT key, value FROM metadata_store").fetchall()
        )
    finally:
        connection.close()
    return {
        "programs": programs,
        "archive": sorted(archive),
        "generation_events": generation_events,
        "attempts": attempts,
        "stop": {
            "candidate_count": len(programs),
            "max_generation": max(program["generation"] for program in programs),
            "last_iteration": int(metadata["last_iteration"]),
        },
        "evaluator_calls": len({program["generation"] for program in programs}),
        "proposal_count": sum(program["generation"] > 0 for program in programs),
    }


def _run_official(task: Path, results: Path, generations: int) -> None:
    _reset_random_state()
    assert upstream_cli.main(
        [
            "--task-dir",
            str(task),
            "--config-fname",
            "shinka.yaml",
            "--results_dir",
            str(results),
            "--num_generations",
            str(generations),
        ]
    ) == 0


def _run_native(task: Path, results: Path, generations: int, run_id: str) -> None:
    _reset_random_state()
    ShinkaNativeEngine().run(
        SearchRunRequest(
            run_id=run_id,
            task_dir=task,
            results_dir=results,
            num_generations=generations,
            config_fname="shinka.yaml",
        )
    )


def test_actual_runner_semantics_match_through_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _make_task(tmp_path)
    official_results = tmp_path / "official"
    native_results = tmp_path / "native"
    monkeypatch.setattr(async_runner, "AsyncLLMClient", _DeterministicAsyncLLMClient)
    monkeypatch.setattr(
        async_runner, "_validate_evo_config_model_env_access", lambda _config: None
    )
    monkeypatch.setattr(
        async_runner, "_print_gradient_logo_and_mirror", lambda *_args, **_kwargs: None
    )
    console_sink = StringIO()

    def utf8_safe_console(*args: Any, **kwargs: Any) -> RichConsole:
        kwargs["file"] = console_sink
        return RichConsole(*args, **kwargs)

    monkeypatch.setattr(async_runner, "Console", utf8_safe_console)

    _run_official(task, official_results, generations=2)
    _run_native(task, native_results, generations=2, run_id="p0-initial")
    initial_official = _canonical_snapshot(official_results)
    initial_native = _canonical_snapshot(native_results)
    assert initial_native == initial_official

    _run_official(task, official_results, generations=6)
    _run_native(task, native_results, generations=6, run_id="p0-resume")
    resumed_official = _canonical_snapshot(official_results)
    resumed_native = _canonical_snapshot(native_results)
    assert resumed_native == resumed_official
    assert resumed_native["stop"] == {
        "candidate_count": 7,
        "max_generation": 5,
        "last_iteration": 5,
    }
    assert resumed_native["evaluator_calls"] == 6
    assert resumed_native["proposal_count"] == 5
    assert any(
        program["archive_inspirations"] or program["top_k_inspirations"]
        for program in resumed_native["programs"]
    )
    assert any(program["migration_history"] for program in resumed_native["programs"])
