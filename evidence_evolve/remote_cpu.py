from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.hashing import sha256_bytes, sha256_file, sha256_object


_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_REMOTE_ROOT = re.compile(r"^/[A-Za-z0-9._/-]+$")
_HOST = re.compile(r"^[A-Za-z0-9._@-]+$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RemoteEntrypoint(str, Enum):
    EVOLVE = "evolve"
    PYTEST = "pytest"
    PYTHON_MODULE = "python-module"


class BoundInput(_StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_base64: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def validate_runtime_content(self) -> "BoundInput":
        if self.content_base64 is None:
            return self
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except ValueError as exc:
            raise ValueError("runtime input content is not valid base64") from exc
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("runtime input exceeds the 2 MiB request limit")
        if sha256_bytes(content) != self.sha256:
            raise ValueError("runtime input content does not match its sha256")
        return self


class RemoteCpuJobRequest(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    created_at_utc: str
    repository_url: str
    repository_commit: str
    entrypoint: RemoteEntrypoint
    argv: tuple[str, ...]
    bound_inputs: tuple[BoundInput, ...] = Field(min_length=1)
    output_paths: tuple[str, ...] = ()
    cpu_workers: int = Field(default=8, ge=1, le=32)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    numeric_threads_per_worker: Literal[1] = 1
    authority: Literal["EXECUTION_ONLY"] = "EXECUTION_ONLY"

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        if not _JOB_ID.fullmatch(value):
            raise ValueError("job_id must be a portable 1-80 character identifier")
        return value

    @field_validator("repository_commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if not _COMMIT.fullmatch(value):
            raise ValueError("repository_commit must be a lowercase Git object id")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("argv must not be empty")
        if any("\x00" in item for item in value):
            raise ValueError("argv must not contain NUL")
        return value

    @field_validator("output_paths")
    @classmethod
    def validate_outputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_path(item) for item in value)


class RemoteCpuJobEnvelope(_StrictModel):
    request: RemoteCpuJobRequest
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RemoteArtifact(_StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class RemoteCpuJobReceipt(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository_commit: str
    state: Literal["SUCCEEDED", "FAILED", "TIMED_OUT"]
    exit_code: int | None
    started_at_utc: str
    completed_at_utc: str
    elapsed_seconds: float = Field(ge=0)
    command: tuple[str, ...]
    environment: dict[str, str]
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pip_freeze_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pip_freeze_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_unchanged: bool
    artifacts: tuple[RemoteArtifact, ...]
    worker_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_transport: Literal["GIT_URL", "GIT_BUNDLE"]
    source_transport_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    authority: Literal["EXECUTION_ONLY"] = "EXECUTION_ONLY"
    observation: Literal[
        "Remote execution receipt only; local verification and the frozen evaluator/gate retain authority."
    ] = (
        "Remote execution receipt only; local verification and the frozen "
        "evaluator/gate retain authority."
    )


class RemoteCpuReceiptEnvelope(_StrictModel):
    receipt: RemoteCpuJobReceipt
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized == "."
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] == ".git"
        or ":" in path.parts[0]
    ):
        raise ValueError(f"path must be repository-relative without traversal: {value}")
    return path.as_posix()


def _run_git(repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
    )


def _https_clone_url(value: str) -> str:
    match = re.fullmatch(r"git@github\.com:(.+)", value)
    if match:
        return f"https://github.com/{match.group(1)}"
    return value


def _create_head_bundle(repo: Path, output: Path, expected_commit: str) -> None:
    actual = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if actual != expected_commit:
        raise ValueError(
            f"source bundle requires the request commit at HEAD: "
            f"request={expected_commit} head={actual}"
        )
    subprocess.run(
        ["git", "-C", str(repo), "bundle", "create", str(output), "HEAD"],
        check=True,
        capture_output=True,
    )


def create_job_request(
    *,
    repo: Path,
    output: Path,
    job_id: str,
    entrypoint: RemoteEntrypoint,
    argv: tuple[str, ...],
    input_paths: tuple[str, ...],
    output_paths: tuple[str, ...],
    cpu_workers: int,
    timeout_seconds: int,
    repository_url: str | None = None,
) -> RemoteCpuJobEnvelope:
    repo = repo.resolve()
    commit = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    origin = repository_url or _run_git(repo, "remote", "get-url", "origin").stdout.strip()
    bound: list[BoundInput] = []
    for raw_path in sorted(set(input_paths)):
        relative = _relative_path(raw_path)
        path = repo / Path(relative)
        if not path.is_file():
            raise ValueError(f"bound input is not a file: {relative}")
        if path.is_symlink():
            raise ValueError(f"bound input must not be a symlink: {relative}")
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--stage", "--error-unmatch", "--", relative],
            check=False,
            capture_output=True,
            text=True,
        )
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--quiet", commit, "--", relative],
            check=False,
        )
        if tracked.returncode == 0 and diff.returncode == 0:
            if tracked.stdout.split(maxsplit=1)[0] == "120000":
                raise ValueError(f"bound input must not be a symlink: {relative}")
            committed = _run_git(repo, "show", f"{commit}:{relative}", text=False).stdout
            bound.append(BoundInput(path=relative, sha256=sha256_bytes(committed)))
        else:
            content = path.read_bytes()
            bound.append(
                BoundInput(
                    path=relative,
                    sha256=sha256_bytes(content),
                    content_base64=base64.b64encode(content).decode("ascii"),
                )
            )
    request = RemoteCpuJobRequest(
        job_id=job_id,
        created_at_utc=_utc_now(),
        repository_url=_https_clone_url(origin),
        repository_commit=commit,
        entrypoint=entrypoint,
        argv=argv,
        bound_inputs=tuple(bound),
        output_paths=tuple(sorted(set(output_paths))),
        cpu_workers=cpu_workers,
        timeout_seconds=timeout_seconds,
    )
    envelope = RemoteCpuJobEnvelope(
        request=request,
        request_sha256=sha256_object(request),
    )
    create_once_json(output, envelope)
    return envelope


def load_job_request(path: Path) -> RemoteCpuJobEnvelope:
    envelope = RemoteCpuJobEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
    actual = sha256_object(envelope.request)
    if actual != envelope.request_sha256:
        raise ValueError(
            f"request hash mismatch: stored={envelope.request_sha256} actual={actual}"
        )
    return envelope


def _available_cpu_count() -> int:
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def _read_first(paths: tuple[Path, ...]) -> str:
    for path in paths:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "unavailable"


def _environment(request: RemoteCpuJobRequest) -> dict[str, str]:
    return {
        "python": sys.version.replace("\n", " "),
        "executable": sys.executable,
        "platform": platform.platform(),
        "available_cpu_count": str(_available_cpu_count()),
        "requested_cpu_workers": str(request.cpu_workers),
        "memory_limit_bytes": _read_first(
            (
                Path("/sys/fs/cgroup/memory.max"),
                Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            )
        ),
        "cpu_quota": _read_first(
            (
                Path("/sys/fs/cgroup/cpu.max"),
                Path("/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us"),
            )
        ),
        "numeric_threads_per_worker": str(request.numeric_threads_per_worker),
    }


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _command(request: RemoteCpuJobRequest) -> list[str]:
    if request.entrypoint is RemoteEntrypoint.PYTHON_MODULE:
        return [sys.executable, "-m", *request.argv]
    module = (
        "evidence_evolve.cli"
        if request.entrypoint is RemoteEntrypoint.EVOLVE
        else "pytest"
    )
    return [sys.executable, "-m", module, *request.argv]


def _validate_worker_ceiling(request: RemoteCpuJobRequest) -> None:
    available = _available_cpu_count()
    if request.cpu_workers > available:
        raise ValueError(
            f"requested workers exceed available CPU affinity: "
            f"requested={request.cpu_workers} available={available}"
        )
    for flag in ("--max-workers", "--workers", "-n"):
        if flag not in request.argv:
            continue
        index = request.argv.index(flag)
        if index + 1 >= len(request.argv):
            raise ValueError(f"worker flag has no value: {flag}")
        try:
            declared = int(request.argv[index + 1])
        except ValueError as exc:
            raise ValueError(f"worker flag must be an integer: {flag}") from exc
        if declared > request.cpu_workers:
            raise ValueError(
                f"command worker count exceeds request ceiling: "
                f"flag={flag} command={declared} ceiling={request.cpu_workers}"
            )


def _copy_artifacts(
    checkout: Path, result: Path, output_paths: tuple[str, ...]
) -> tuple[RemoteArtifact, ...]:
    artifacts: list[RemoteArtifact] = []
    artifact_root = result / "artifacts"
    for relative in output_paths:
        source = checkout / Path(relative)
        if not source.exists():
            raise FileNotFoundError(f"declared output was not produced: {relative}")
        if source.is_symlink():
            raise ValueError(f"declared output must not be a symlink: {relative}")
        descendants = list(source.rglob("*")) if source.is_dir() else []
        if any(item.is_symlink() for item in descendants):
            raise ValueError(f"output tree must not contain symlinks: {relative}")
        files = [source] if source.is_file() else sorted(
            item for item in descendants if item.is_file()
        )
        if not files:
            raise ValueError(f"declared output contains no files: {relative}")
        for item in files:
            if item.is_symlink():
                raise ValueError(f"output artifact must not be a symlink: {item}")
            item_relative = item.relative_to(checkout).as_posix()
            destination = artifact_root / Path(item_relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
            artifacts.append(
                RemoteArtifact(
                    path=item_relative,
                    sha256=sha256_file(destination),
                    size_bytes=destination.stat().st_size,
                )
            )
    return tuple(artifacts)


def execute_job(
    request_path: Path, job_root: Path, *, source_bundle: Path | None = None
) -> Path:
    envelope = load_job_request(request_path)
    request = envelope.request
    job_dir = job_root.resolve() / request.job_id
    result = job_dir / "result"
    receipt_path = result / "receipt.json"
    if receipt_path.exists():
        receipt = load_job_receipt(receipt_path)
        if receipt.receipt.request_sha256 != envelope.request_sha256:
            raise ValueError("existing job result is bound to a different request")
        return result
    if job_dir.exists():
        raise FileExistsError(f"incomplete job directory already exists: {job_dir}")
    result.mkdir(parents=True)
    shutil.copy2(request_path, result / "job-request.json")
    checkout = job_dir / "checkout"
    stdout_path = result / "stdout.log"
    stderr_path = result / "stderr.log"
    freeze_before_path = result / "pip-freeze-before.txt"
    freeze_after_path = result / "pip-freeze-after.txt"
    started_at = _utc_now()
    started = time.monotonic()
    exit_code: int | None = None
    state: Literal["SUCCEEDED", "FAILED", "TIMED_OUT"] = "FAILED"
    command = _command(request)
    environment = _environment(request)
    source_transport: Literal["GIT_URL", "GIT_BUNDLE"] = "GIT_URL"
    source_transport_sha256: str | None = None
    freeze_before = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        check=False,
        capture_output=True,
    ).stdout
    freeze_before_path.write_bytes(freeze_before)
    try:
        _validate_worker_ceiling(request)
        source = request.repository_url
        if source_bundle is not None:
            source_bundle = source_bundle.resolve()
            if not source_bundle.is_file():
                raise FileNotFoundError(f"source bundle does not exist: {source_bundle}")
            subprocess.run(
                ["git", "bundle", "list-heads", str(source_bundle)],
                check=True,
                capture_output=True,
            )
            source = str(source_bundle)
            source_transport = "GIT_BUNDLE"
            source_transport_sha256 = sha256_file(source_bundle)
        subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
        _run_git(checkout, "remote", "add", "origin", source)
        _run_git(
            checkout,
            "fetch",
            "--depth",
            "1",
            "origin",
            request.repository_commit,
        )
        _run_git(checkout, "config", "core.autocrlf", "false")
        _run_git(checkout, "checkout", "--detach", "FETCH_HEAD")
        actual_commit = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
        if actual_commit != request.repository_commit:
            raise ValueError(
                f"checkout mismatch: expected={request.repository_commit} actual={actual_commit}"
            )
        for bound in request.bound_inputs:
            bound_path = checkout / Path(bound.path)
            if bound.content_base64 is not None:
                bound_path.parent.mkdir(parents=True, exist_ok=True)
                bound_path.write_bytes(base64.b64decode(bound.content_base64, validate=True))
            if bound_path.is_symlink():
                raise ValueError(f"bound input must not be a symlink: {bound.path}")
            actual = sha256_file(bound_path)
            if actual != bound.sha256:
                raise ValueError(
                    f"bound input hash mismatch for {bound.path}: "
                    f"expected={bound.sha256} actual={actual}"
                )
        env = os.environ.copy()
        thread_value = str(request.numeric_threads_per_worker)
        env.update(
            {
                "OMP_NUM_THREADS": thread_value,
                "OPENBLAS_NUM_THREADS": thread_value,
                "MKL_NUM_THREADS": thread_value,
                "NUMEXPR_NUM_THREADS": thread_value,
                "EVIDENCE_EVOLVE_CPU_WORKERS": str(request.cpu_workers),
                "PYTHONPATH": str(checkout)
                + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            }
        )
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=checkout,
                env=env,
                stdout=stdout,
                stderr=stderr,
                **popen_kwargs,
            )
            try:
                exit_code = process.wait(timeout=request.timeout_seconds)
                state = "SUCCEEDED" if exit_code == 0 else "FAILED"
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                exit_code = process.poll()
                state = "TIMED_OUT"
    except Exception as exc:
        with stderr_path.open("ab") as stderr:
            stderr.write(f"\nREMOTE_WORKER_ERROR: {type(exc).__name__}: {exc}\n".encode())
    freeze_after = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        check=False,
        capture_output=True,
    ).stdout
    freeze_after_path.write_bytes(freeze_after)
    environment_unchanged = freeze_before == freeze_after
    if not environment_unchanged:
        state = "FAILED"
        with stderr_path.open("ab") as stderr:
            stderr.write(b"\nREMOTE_ENVIRONMENT_ERROR: pip environment changed during job\n")
    stdout_path.touch(exist_ok=True)
    stderr_path.touch(exist_ok=True)
    artifacts: tuple[RemoteArtifact, ...] = ()
    if checkout.exists():
        try:
            artifacts = _copy_artifacts(checkout, result, request.output_paths)
        except Exception as exc:
            state = "FAILED"
            with stderr_path.open("ab") as stderr:
                stderr.write(
                    f"\nREMOTE_ARTIFACT_ERROR: {type(exc).__name__}: {exc}\n".encode()
                )
    receipt = RemoteCpuJobReceipt(
        job_id=request.job_id,
        request_sha256=envelope.request_sha256,
        repository_commit=request.repository_commit,
        state=state,
        exit_code=exit_code,
        started_at_utc=started_at,
        completed_at_utc=_utc_now(),
        elapsed_seconds=time.monotonic() - started,
        command=tuple(command),
        environment=environment,
        stdout_sha256=sha256_file(stdout_path),
        stderr_sha256=sha256_file(stderr_path),
        pip_freeze_before_sha256=sha256_file(freeze_before_path),
        pip_freeze_after_sha256=sha256_file(freeze_after_path),
        environment_unchanged=environment_unchanged,
        artifacts=artifacts,
        worker_code_sha256=sha256_file(Path(__file__)),
        source_transport=source_transport,
        source_transport_sha256=source_transport_sha256,
    )
    create_once_json(
        receipt_path,
        RemoteCpuReceiptEnvelope(
            receipt=receipt,
            receipt_sha256=sha256_object(receipt),
        ),
    )
    return result


def load_job_receipt(path: Path) -> RemoteCpuReceiptEnvelope:
    envelope = RemoteCpuReceiptEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
    actual = sha256_object(envelope.receipt)
    if actual != envelope.receipt_sha256:
        raise ValueError(
            f"receipt hash mismatch: stored={envelope.receipt_sha256} actual={actual}"
        )
    return envelope


def verify_result(request_path: Path, result_dir: Path) -> RemoteCpuReceiptEnvelope:
    request = load_job_request(request_path)
    returned_request = load_job_request(result_dir / "job-request.json")
    if returned_request.request_sha256 != request.request_sha256:
        raise ValueError("returned job request does not match local request")
    receipt = load_job_receipt(result_dir / "receipt.json")
    if receipt.receipt.job_id != request.request.job_id:
        raise ValueError("receipt job_id does not match request")
    if receipt.receipt.request_sha256 != request.request_sha256:
        raise ValueError("receipt is bound to a different request")
    if receipt.receipt.repository_commit != request.request.repository_commit:
        raise ValueError("receipt repository_commit does not match request")
    expected_files = {
        "stdout.log": receipt.receipt.stdout_sha256,
        "stderr.log": receipt.receipt.stderr_sha256,
        "pip-freeze-before.txt": receipt.receipt.pip_freeze_before_sha256,
        "pip-freeze-after.txt": receipt.receipt.pip_freeze_after_sha256,
    }
    for relative, expected in expected_files.items():
        actual = sha256_file(result_dir / relative)
        if actual != expected:
            raise ValueError(f"result hash mismatch for {relative}")
    for artifact in receipt.receipt.artifacts:
        path = result_dir / "artifacts" / Path(artifact.path)
        if sha256_file(path) != artifact.sha256 or path.stat().st_size != artifact.size_bytes:
            raise ValueError(f"result artifact mismatch for {artifact.path}")
    artifact_paths = {item.path for item in receipt.receipt.artifacts}
    for declared in request.request.output_paths:
        if not any(path == declared or path.startswith(f"{declared}/") for path in artifact_paths):
            raise ValueError(f"declared output has no verified artifact: {declared}")
    if not receipt.receipt.environment_unchanged:
        raise ValueError("remote Python environment changed during job")
    return receipt


def _validate_remote(host: str, remote_root: str, port: int) -> None:
    if not _HOST.fullmatch(host):
        raise ValueError("host contains unsupported characters")
    if not _REMOTE_ROOT.fullmatch(remote_root) or ".." in PurePosixPath(remote_root).parts:
        raise ValueError("remote_root must be a simple absolute POSIX path")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")


def bootstrap_remote(
    *,
    repo: Path,
    host: str,
    port: int,
    remote_root: str,
    repository_commit: str,
    remote_python: str,
) -> subprocess.CompletedProcess[str]:
    _validate_remote(host, remote_root, port)
    if not _COMMIT.fullmatch(repository_commit):
        raise ValueError("repository_commit must be a lowercase Git object id")
    for path in (remote_python,):
        if not _REMOTE_ROOT.fullmatch(path):
            raise ValueError("remote_python must be an absolute POSIX path")
    with tempfile.TemporaryDirectory(prefix="ee-remote-bootstrap-") as temporary:
        bundle = Path(temporary) / "control.bundle"
        _create_head_bundle(repo.resolve(), bundle, repository_commit)
        remote_bundle = f"{remote_root}/control.bundle"
        subprocess.run(
            [
                "ssh",
                "-p",
                str(port),
                "-o",
                "BatchMode=yes",
                host,
                f"mkdir -p {shlex.quote(remote_root)}",
            ],
            check=True,
        )
        subprocess.run(
            ["scp", "-P", str(port), str(bundle), f"{host}:{remote_bundle}"],
            check=True,
        )
        root = shlex.quote(remote_root)
        control = shlex.quote(f"{remote_root}/control")
        venv = shlex.quote(f"{remote_root}/venv")
        bundle_arg = shlex.quote(remote_bundle)
        commit = shlex.quote(repository_commit)
        python = shlex.quote(remote_python)
        script = (
            "set -eu; "
            f"mkdir -p {root}; "
            f"git bundle list-heads {bundle_arg} >/dev/null; "
            f"if [ ! -d {control}/.git ]; then git clone {bundle_arg} {control}; fi; "
            f"test -z \"$(git -C {control} status --porcelain)\"; "
            f"git -C {control} fetch {bundle_arg} {commit}; "
            f"git -C {control} checkout --detach {commit}; "
            f"if [ ! -x {venv}/bin/python ]; then {python} -m venv {venv}; fi; "
            f"{venv}/bin/python -m pip install -e {control}'[dev,algotune-portfolio,onnx-canary]'; "
            f"cd {control}; {venv}/bin/python -m evidence_evolve.cli backend-status"
        )
        return subprocess.run(
            ["ssh", "-p", str(port), "-o", "BatchMode=yes", host, script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )


def dispatch_job(
    *,
    repo: Path,
    request_path: Path,
    host: str,
    port: int,
    remote_root: str,
    local_result_dir: Path,
) -> RemoteCpuReceiptEnvelope:
    _validate_remote(host, remote_root, port)
    envelope = load_job_request(request_path)
    if local_result_dir.exists():
        raise FileExistsError(f"local result directory already exists: {local_result_dir}")
    inbox = f"{remote_root}/inbox"
    remote_request = f"{inbox}/{envelope.request.job_id}.json"
    subprocess.run(
        [
            "ssh",
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            host,
            f"mkdir -p {shlex.quote(inbox)}",
        ],
        check=True,
    )
    remote_bundle = f"{inbox}/{envelope.request.job_id}.bundle"
    with tempfile.TemporaryDirectory(prefix="ee-remote-dispatch-") as temporary:
        bundle = Path(temporary) / "source.bundle"
        _create_head_bundle(repo.resolve(), bundle, envelope.request.repository_commit)
        subprocess.run(
            ["scp", "-P", str(port), str(request_path), f"{host}:{remote_request}"],
            check=True,
        )
        subprocess.run(
            ["scp", "-P", str(port), str(bundle), f"{host}:{remote_bundle}"],
            check=True,
        )
        control = f"{remote_root}/control"
        python = f"{remote_root}/venv/bin/python"
        jobs = f"{remote_root}/jobs"
        remote_command = (
            f"cd {shlex.quote(control)}; {shlex.quote(python)} "
            f"-m evidence_evolve.remote_cpu execute-worker {shlex.quote(remote_request)} "
            f"--job-root {shlex.quote(jobs)} "
            f"--source-bundle {shlex.quote(remote_bundle)}"
        )
        subprocess.run(
            ["ssh", "-p", str(port), "-o", "BatchMode=yes", host, remote_command],
            check=True,
            timeout=envelope.request.timeout_seconds + 300,
        )
    local_result_dir.parent.mkdir(parents=True, exist_ok=True)
    remote_result = f"{jobs}/{envelope.request.job_id}/result"
    subprocess.run(
        ["scp", "-P", str(port), "-r", f"{host}:{remote_result}", str(local_result_dir)],
        check=True,
    )
    return verify_result(request_path, local_result_dir)


def _repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("cannot locate Git repository; pass --repo")
    return Path(completed.stdout.strip()).resolve()


def _print(value: object) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolve-remote",
        description="Hash-bound execution-only SSH CPU worker",
    )
    parser.add_argument("--repo", help="Git repository root (default: auto-detect)")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-job", help="create a bound job request")
    create.add_argument("--job-id", required=True)
    create.add_argument(
        "--entrypoint", choices=[item.value for item in RemoteEntrypoint], required=True
    )
    create.add_argument("--input", action="append", default=[])
    create.add_argument("--output-path", action="append", default=[])
    create.add_argument("--workers", type=int, default=8)
    create.add_argument("--timeout-seconds", type=int, default=1800)
    create.add_argument("--repository-url")
    create.add_argument("--output", required=True)
    create.add_argument("remote_args", nargs=argparse.REMAINDER)

    bootstrap = commands.add_parser(
        "bootstrap", help="install the current committed worker control plane"
    )
    bootstrap.add_argument("--host", required=True)
    bootstrap.add_argument("--port", type=int, default=22)
    bootstrap.add_argument(
        "--remote-root", default="/root/autodl-tmp/evidence-evolve-worker"
    )
    bootstrap.add_argument(
        "--remote-python", default="/root/miniconda3/bin/python"
    )

    dispatch = commands.add_parser("dispatch", help="run and retrieve one job")
    dispatch.add_argument("request")
    dispatch.add_argument("--host", required=True)
    dispatch.add_argument("--port", type=int, default=22)
    dispatch.add_argument(
        "--remote-root", default="/root/autodl-tmp/evidence-evolve-worker"
    )
    dispatch.add_argument("--result-dir", required=True)

    verify = commands.add_parser("verify-result", help="verify a result bundle")
    verify.add_argument("request")
    verify.add_argument("result_dir")

    execute = commands.add_parser("execute-worker", help=argparse.SUPPRESS)
    execute.add_argument("request")
    execute.add_argument("--job-root", required=True)
    execute.add_argument("--source-bundle")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create-job":
            command_args = tuple(args.remote_args)
            if command_args[:1] == ("--",):
                command_args = command_args[1:]
            envelope = create_job_request(
                repo=_repo_root(args.repo),
                output=Path(args.output).resolve(),
                job_id=args.job_id,
                entrypoint=RemoteEntrypoint(args.entrypoint),
                argv=command_args,
                input_paths=tuple(args.input),
                output_paths=tuple(args.output_path),
                cpu_workers=args.workers,
                timeout_seconds=args.timeout_seconds,
                repository_url=args.repository_url,
            )
            _print(envelope)
            return 0
        if args.command == "bootstrap":
            repo = _repo_root(args.repo)
            commit = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
            completed = bootstrap_remote(
                repo=repo,
                host=args.host,
                port=args.port,
                remote_root=args.remote_root,
                repository_commit=commit,
                remote_python=args.remote_python,
            )
            _print(
                {
                    "repository_commit": commit,
                    "remote_root": args.remote_root,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            return 0
        if args.command == "dispatch":
            receipt = dispatch_job(
                repo=_repo_root(args.repo),
                request_path=Path(args.request).resolve(),
                host=args.host,
                port=args.port,
                remote_root=args.remote_root,
                local_result_dir=Path(args.result_dir).resolve(),
            )
            _print(receipt)
            return 0 if receipt.receipt.state == "SUCCEEDED" else 1
        if args.command == "verify-result":
            receipt = verify_result(
                Path(args.request).resolve(), Path(args.result_dir).resolve()
            )
            _print(receipt)
            return 0 if receipt.receipt.state == "SUCCEEDED" else 1
        result = execute_job(
            Path(args.request),
            Path(args.job_root),
            source_bundle=Path(args.source_bundle) if args.source_bundle else None,
        )
        _print({"result_dir": str(result)})
        return 0
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        payload = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, subprocess.CalledProcessError):
            payload["stdout"] = exc.stdout
            payload["stderr"] = exc.stderr
        _print(payload)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
