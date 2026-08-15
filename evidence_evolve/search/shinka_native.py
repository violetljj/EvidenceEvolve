from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from evidence_evolve.artifacts import create_once_json, environment_receipt
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.search.models import (
    SearchRunReceipt,
    SearchRunRequest,
    ShinkaImportSummary,
)


SHINKA_DISTRIBUTION = "shinka-evolve"
SHINKA_VERSION = "0.0.7"
SHINKA_SOURCE_COMMIT = "c4568adde253cacf185be3a8412c3c2142761ebe"


def _nested_number(payload: Any, key: str) -> float:
    if isinstance(payload, dict):
        total = float(payload.get(key, 0) or 0)
        return total + sum(_nested_number(value, key) for value in payload.values())
    if isinstance(payload, list):
        return sum(_nested_number(value, key) for value in payload)
    return 0.0


def _read_json_value(value: str | None) -> Any:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    return payload


def _read_json_object(value: str | None) -> dict[str, Any]:
    payload = _read_json_value(value)
    return payload if isinstance(payload, dict) else {}


def import_shinka_run(results_dir: Path) -> ShinkaImportSummary:
    """Import native SQLite state without changing Shinka's database or score."""

    database = results_dir / "programs.sqlite"
    if not database.is_file():
        raise FileNotFoundError(f"native Shinka database not found: {database}")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, parent_id, archive_inspiration_ids, "
            "top_k_inspiration_ids, generation, combined_score, public_metrics, "
            "private_metrics, correct, metadata FROM programs ORDER BY generation, id"
        ).fetchall()
        if not rows:
            raise ValueError("native Shinka database contains no programs")
        archive_count = int(
            connection.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        )
        attempt_count = int(
            connection.execute("SELECT COUNT(*) FROM attempt_log").fetchone()[0]
        )
        generation_event_count = int(
            connection.execute("SELECT COUNT(*) FROM generation_event_log").fetchone()[0]
        )
        metric_events = [
            {
                "id": row["id"],
                "generation": row["generation"],
                "combined_score": row["combined_score"],
                "public_metrics": _read_json_object(row["public_metrics"]),
                "private_metrics": _read_json_object(row["private_metrics"]),
                "correct": bool(row["correct"]),
            }
            for row in rows
        ]
        lineage = [
            {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "archive_inspiration_ids": _read_json_value(
                    row["archive_inspiration_ids"]
                ),
                "top_k_inspiration_ids": _read_json_value(
                    row["top_k_inspiration_ids"]
                ),
                "generation": row["generation"],
            }
            for row in rows
        ]
        metadata_payloads = [_read_json_object(row["metadata"]) for row in rows]
        correct_rows = [row for row in rows if bool(row["correct"])]
        eligible = correct_rows or rows
        best = max(eligible, key=lambda row: float(row["combined_score"] or 0.0))
        total_api_cost = sum(
            float(payload.get("api_costs", 0) or 0)
            + float(payload.get("embed_cost", 0) or 0)
            + float(payload.get("novelty_cost", 0) or 0)
            + float(payload.get("meta_cost", 0) or 0)
            for payload in metadata_payloads
        )
        input_tokens = int(
            sum(_nested_number(payload, "input_tokens") for payload in metadata_payloads)
        )
        output_tokens = int(
            sum(_nested_number(payload, "output_tokens") for payload in metadata_payloads)
        )
        return ShinkaImportSummary(
            database_path=str(database.resolve()),
            candidate_count=len(rows),
            correct_candidate_count=len(correct_rows),
            archive_count=archive_count,
            max_generation=max(int(row["generation"]) for row in rows),
            best_program_id=str(best["id"]),
            best_combined_score=float(best["combined_score"] or 0.0),
            total_api_cost=total_api_cost,
            input_tokens_observed=input_tokens,
            output_tokens_observed=output_tokens,
            invalid_candidate_rate=1.0 - (len(correct_rows) / len(rows)),
            attempt_event_count=attempt_count,
            generation_event_count=generation_event_count,
            lineage_sha256=sha256_object(lineage),
            metric_event_sha256=sha256_object(metric_events),
        )
    finally:
        connection.close()


def _artifact_hashes(results_dir: Path) -> dict[str, str]:
    names = (
        "programs.sqlite",
        "programs.sqlite-wal",
        "programs.sqlite-shm",
        "prompts.sqlite",
        "prompts.sqlite-wal",
        "prompts.sqlite-shm",
        "pricing_snapshot.json",
        "evolution_run.log",
        "evaluate.py",
    )
    hashes: dict[str, str] = {}
    for name in names:
        path = results_dir / name
        if path.is_file():
            hashes[name] = sha256_file(path)
    for path in sorted(results_dir.glob("init_program.*")):
        if path.is_file():
            hashes[path.name] = sha256_file(path)
    return hashes


class ShinkaNativeEngine:
    """Run the pinned official Shinka runner without replacing search behavior."""

    def __init__(self, runner_factory: Callable[..., Any] | None = None) -> None:
        self.runner_factory = runner_factory

    def _require_upstream(self) -> None:
        try:
            installed = metadata.version(SHINKA_DISTRIBUTION)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                "ShinkaEvolve is not installed; install the project's 'shinka' extra"
            ) from exc
        if installed != SHINKA_VERSION:
            raise RuntimeError(
                f"ShinkaEvolve version mismatch: expected={SHINKA_VERSION} actual={installed}"
            )
        distribution = metadata.distribution(SHINKA_DISTRIBUTION)
        direct_url = distribution.read_text("direct_url.json")
        source_commit: str | None = None
        if direct_url:
            try:
                payload = json.loads(direct_url)
            except json.JSONDecodeError:
                payload = {}
            vcs_info = payload.get("vcs_info", {})
            if isinstance(vcs_info, dict):
                value = vcs_info.get("commit_id")
                if isinstance(value, str):
                    source_commit = value
        if source_commit != SHINKA_SOURCE_COMMIT:
            raise RuntimeError(
                "ShinkaEvolve source commit is not verified: "
                f"expected={SHINKA_SOURCE_COMMIT} actual={source_commit}"
            )

    def _runner_kwargs(
        self, request: SearchRunRequest
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Apply the pinned upstream shinka_run construction path verbatim."""

        import shinka.cli.run as upstream

        task_dir = request.task_dir
        results_dir = request.results_dir
        evaluate_path, initial_path = upstream._validate_task_dir(task_dir)
        language = upstream._infer_language(initial_path)
        allowed_types = upstream._field_types()
        file_overrides, runner_config = upstream.load_optional_yaml_config(
            task_dir=task_dir,
            config_fname=request.config_fname,
            allowed_field_types=allowed_types,
        )
        parsed_overrides = upstream._parse_overrides(
            request.set_overrides, allowed_types
        )

        evo_values = upstream._build_default_evo_values(
            language=language,
            init_program_path=initial_path,
            results_dir=results_dir,
            num_generations=request.num_generations,
        )
        evo_values.update(file_overrides["evo"])
        evo_values.update(parsed_overrides["evo"])
        evo_values["results_dir"] = str(results_dir)
        evo_values["num_generations"] = request.num_generations

        db_values = upstream._build_default_db_values()
        db_values.update(file_overrides["db"])
        db_values.update(parsed_overrides["db"])

        job_values = upstream._build_default_job_values(evaluate_path)
        job_values.update(file_overrides["job"])
        job_values.update(parsed_overrides["job"])

        max_evaluation_jobs = request.max_evaluation_jobs
        if max_evaluation_jobs is None:
            max_evaluation_jobs = runner_config.get("max_evaluation_jobs")
        max_proposal_jobs = request.max_proposal_jobs
        if max_proposal_jobs is None:
            max_proposal_jobs = runner_config.get("max_proposal_jobs")
        max_db_workers = request.max_db_workers
        if max_db_workers is None:
            max_db_workers = runner_config.get("max_db_workers")
        verbose = upstream._resolve_runner_bool(
            request.verbose, runner_config, "verbose", True
        )
        debug = request.debug or bool(runner_config.get("debug", False))

        evo_config = upstream.EvolutionConfig(**evo_values)
        db_config = upstream.DatabaseConfig(**db_values)
        job_config = upstream.LocalJobConfig(**job_values)
        kwargs: dict[str, Any] = {
            "evo_config": evo_config,
            "job_config": job_config,
            "db_config": db_config,
            "banner_style": "minimal",
            "verbose": verbose,
            "debug": debug,
            "init_program_str": initial_path.read_text(encoding="utf-8"),
            "evaluate_str": evaluate_path.read_text(encoding="utf-8"),
        }
        if max_evaluation_jobs is not None:
            kwargs["max_evaluation_jobs"] = max_evaluation_jobs
        if max_proposal_jobs is not None:
            kwargs["max_proposal_jobs"] = max_proposal_jobs
        if max_db_workers is not None:
            kwargs["max_db_workers"] = max_db_workers
        effective = {
            "evo_config": asdict(evo_config),
            "db_config": asdict(db_config),
            "job_config": asdict(job_config),
            "runner": {
                key: value
                for key, value in kwargs.items()
                if key
                not in {
                    "evo_config",
                    "db_config",
                    "job_config",
                    "init_program_str",
                    "evaluate_str",
                }
            },
        }
        return kwargs, effective

    def run(self, request: SearchRunRequest) -> SearchRunReceipt:
        self._require_upstream()
        receipt_path = (
            request.results_dir
            / "evidence_evolve"
            / "receipts"
            / f"{request.run_id}.json"
        )
        if receipt_path.exists():
            raise FileExistsError(f"search run receipt already exists: {receipt_path}")
        kwargs, effective = self._runner_kwargs(request)

        if self.runner_factory is None:
            from shinka.core import ShinkaEvolveRunner

            runner_factory = ShinkaEvolveRunner
        else:
            runner_factory = self.runner_factory
        runner = runner_factory(**kwargs)
        runner.run()

        imported = import_shinka_run(request.results_dir)
        task_assets = {
            path.name: sha256_file(path)
            for path in sorted(request.task_dir.glob("*"))
            if path.is_file() and (path.name == "evaluate.py" or path.name.startswith("initial."))
        }
        config_fields = {
            namespace: sorted(values)
            for namespace, values in effective.items()
        }
        receipt = SearchRunReceipt(
            run_id=request.run_id,
            request_sha256=sha256_object(request),
            effective_config_sha256=sha256_object(effective),
            effective_config_fields=config_fields,
            task_asset_hashes=task_assets,
            upstream_artifact_hashes=_artifact_hashes(request.results_dir),
            results_dir=str(request.results_dir),
            environment=environment_receipt(
                {
                    "shinka_distribution": SHINKA_DISTRIBUTION,
                    "shinka_version": SHINKA_VERSION,
                    "shinka_source_commit": SHINKA_SOURCE_COMMIT,
                }
            ),
            imported=imported,
            metadata={
                "adapter_behavior": "UPSTREAM_CLI_CONSTRUCTION_AND_NATIVE_RUNNER",
                "upstream_database_preserved": True,
                "upstream_webui_compatible": True,
                "confirmation_evidence_imported": False,
            },
        )
        create_once_json(receipt_path, receipt)
        return receipt
