from __future__ import annotations

import atexit
import base64
import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from evidence_evolve.hashing import sha256_file, sha256_object


_SSH_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectionAttempts=3",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
)


class PersistentRemoteEvaluator:
    """One long-lived SSH process carrying newline-delimited evaluation RPCs."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        remote_root: str,
        runner_module: str,
        repository_commit: str,
        protocol_sha256: str,
    ) -> None:
        self._runner_module = runner_module
        self._repository_commit = repository_commit
        self._protocol_sha256 = protocol_sha256
        control = f"{remote_root}/control"
        remote_command = (
            f"cd {control} && "
            f"test \"$(git rev-parse HEAD)\" = {repository_commit} && "
            f"PYTHONPATH={control} {remote_root}/venv/bin/python -u "
            "-m evidence_evolve.remote_eval_rpc"
        )
        self._process = subprocess.Popen(
            ["ssh", "-p", str(port), *_SSH_OPTIONS, host, remote_command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._stderr_tail: list[str] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        atexit.register(self.close)

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._responses.put(line)
        self._responses.put(None)

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr_tail.append(line.rstrip())
            del self._stderr_tail[:-20]

    def evaluate(
        self,
        *,
        task: str,
        candidate: Path,
        seeds: list[int],
        repeats: int,
        workers: int,
        cold: bool,
        context: str,
        timeout_seconds: float = 1800.0,
    ) -> dict[str, Any]:
        candidate_bytes = candidate.read_bytes()
        payload = {
            "schema_version": "1.0",
            "runner_module": self._runner_module,
            "repository_commit": self._repository_commit,
            "protocol_sha256": self._protocol_sha256,
            "task": task,
            "candidate_base64": base64.b64encode(candidate_bytes).decode("ascii"),
            "candidate_sha256": sha256_file(candidate),
            "seeds": seeds,
            "repeats": repeats,
            "workers": workers,
            "cold": cold,
            "context": context,
        }
        payload["request_id"] = sha256_object(payload)
        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError(self._failure("persistent SSH evaluator exited"))
            assert self._process.stdin is not None
            self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
            try:
                line = self._responses.get(timeout=timeout_seconds)
            except queue.Empty as exc:
                raise TimeoutError(self._failure("persistent evaluation timed out")) from exc
            if line is None:
                raise RuntimeError(self._failure("persistent SSH evaluator closed"))
        response = json.loads(line)
        if response.get("request_id") != payload["request_id"]:
            raise ValueError("persistent evaluation response id mismatch")
        if response.get("state") != "SUCCEEDED":
            raise RuntimeError(
                f"persistent remote evaluation failed: {response.get('error_class')}: "
                f"{response.get('error')}"
            )
        result = dict(response["result"])
        result["remote_receipt_sha256"] = response["receipt_sha256"]
        result["remote_transport"] = "PERSISTENT_SSH_RPC"
        return result

    def _failure(self, message: str) -> str:
        detail = " | ".join(self._stderr_tail[-5:])
        return f"{message}: {detail}" if detail else message

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()


_CLIENT: PersistentRemoteEvaluator | None = None


def persistent_evaluate(
    *,
    candidate: Path,
    task: str,
    seeds: list[int],
    repeats: int,
    workers: int,
    cold: bool,
    context: str,
    runner_module: str,
    repository_commit: str,
    protocol_sha256: str,
) -> dict[str, Any]:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = PersistentRemoteEvaluator(
            host=os.environ.get("EE_REMOTE_HOST", "root@connect.westb.seetacloud.com"),
            port=int(os.environ.get("EE_REMOTE_PORT", "16288")),
            remote_root=os.environ.get(
                "EE_REMOTE_ROOT", "/root/autodl-tmp/evidence-evolve-worker"
            ),
            runner_module=runner_module,
            repository_commit=repository_commit,
            protocol_sha256=protocol_sha256,
        )
    return _CLIENT.evaluate(
        task=task,
        candidate=candidate,
        seeds=seeds,
        repeats=repeats,
        workers=workers,
        cold=cold,
        context=context,
    )
