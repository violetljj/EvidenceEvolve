from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import evidence_evolve.remote_cpu as remote_cpu

from evidence_evolve.remote_cpu import (
    RemoteEntrypoint,
    create_job_request,
    execute_job,
    verify_result,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_create_job_embeds_dirty_bound_input_as_hash_bound_runtime_content(
    tmp_path: Path,
) -> None:
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

    request = create_job_request(
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

    assert request.request.bound_inputs[0].content_base64 is not None


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


def test_transport_setup_retries_only_ssh_exit_255(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def flaky_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise subprocess.CalledProcessError(255, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(remote_cpu.subprocess, "run", flaky_run)
    monkeypatch.setattr(remote_cpu.time, "sleep", sleeps.append)

    completed = remote_cpu._run_transport_command(["ssh", "example"], attempts=3)

    assert completed.returncode == 0
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_transport_setup_does_not_retry_remote_non_transport_failure(
    monkeypatch,
) -> None:
    calls = 0

    def failed_run(command, **kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(remote_cpu.subprocess, "run", failed_run)

    with pytest.raises(subprocess.CalledProcessError):
        remote_cpu._run_transport_command(["scp", "example"], attempts=3)
    assert calls == 1
