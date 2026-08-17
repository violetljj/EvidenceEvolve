from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evidence_evolve.remote_cpu import (
    RemoteEntrypoint,
    create_job_request,
    execute_job,
    verify_result,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_create_job_refuses_dirty_bound_input(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "remote-test@example.invalid")
    _git(repo, "config", "user.name", "Remote Test")
    source = repo / "input.txt"
    source.write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "input.txt")
    _git(repo, "commit", "-m", "fixture")
    source.write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="differs from repository_commit"):
        create_job_request(
            repo=repo,
            output=tmp_path / "job.json",
            job_id="DIRTY-INPUT",
            entrypoint=RemoteEntrypoint.PYTEST,
            argv=("-q",),
            input_paths=("input.txt",),
            output_paths=(),
            cpu_workers=1,
            timeout_seconds=30,
            repository_url=str(repo),
        )


def test_worker_result_is_commit_bound_and_tamper_evident(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    request_path = tmp_path / "request.json"
    request = create_job_request(
        repo=repo,
        output=request_path,
        job_id="REMOTE-CPU-CANARY",
        entrypoint=RemoteEntrypoint.EVOLVE,
        argv=("export-schemas", "--output-dir", "remote-output"),
        input_paths=("evidence_evolve/hashing.py",),
        output_paths=("remote-output",),
        cpu_workers=8,
        timeout_seconds=120,
        repository_url="https://invalid.example/EvidenceEvolve.git",
    )

    bundle = tmp_path / "source.bundle"
    subprocess.run(
        ["git", "-C", str(repo), "bundle", "create", str(bundle), "HEAD"],
        check=True,
        capture_output=True,
    )
    result_dir = execute_job(
        request_path, tmp_path / "jobs", source_bundle=bundle
    )
    verified = verify_result(request_path, result_dir)

    assert verified.receipt.state == "SUCCEEDED"
    assert verified.receipt.authority == "EXECUTION_ONLY"
    assert verified.receipt.request_sha256 == request.request_sha256
    assert verified.receipt.repository_commit == request.request.repository_commit
    assert verified.receipt.environment["requested_cpu_workers"] == "8"
    assert verified.receipt.source_transport == "GIT_BUNDLE"
    assert verified.receipt.source_transport_sha256 is not None
    assert any(
        artifact.path == "remote-output/research_contract.schema.json"
        for artifact in verified.receipt.artifacts
    )

    with (result_dir / "stdout.log").open("ab") as stream:
        stream.write(b"tampered\n")
    with pytest.raises(ValueError, match="stdout.log"):
        verify_result(request_path, result_dir)
