from __future__ import annotations

import base64
import importlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_evolve.hashing import sha256_bytes, sha256_file, sha256_object


REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def handle(request: dict[str, Any]) -> dict[str, Any]:
    request_id = str(request["request_id"])
    if request_id != sha256_object({k: v for k, v in request.items() if k != "request_id"}):
        raise ValueError("persistent request hash mismatch")
    commit = _git_commit()
    if request["repository_commit"] != commit:
        raise ValueError("persistent evaluator commit drift")
    candidate_bytes = base64.b64decode(request["candidate_base64"], validate=True)
    if sha256_bytes(candidate_bytes) != request["candidate_sha256"]:
        raise ValueError("persistent candidate hash mismatch")
    module = importlib.import_module(str(request["runner_module"]))
    if hasattr(module, "_install_context"):
        module._install_context()
    protocol = Path(module.PROTOCOL)
    if sha256_file(protocol) != request["protocol_sha256"]:
        raise ValueError("persistent evaluator protocol drift")
    started = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="ee-persistent-eval-") as temporary:
        root = Path(temporary)
        candidate = root / "candidate.py"
        seeds = root / "seeds.json"
        output = root / "output.json"
        candidate.write_bytes(candidate_bytes)
        seeds.write_text(json.dumps({"seeds": request["seeds"]}), encoding="utf-8")
        result = module.run_remote_evaluator(
            task_name=str(request["task"]),
            candidate=candidate,
            seeds_path=seeds,
            repeats=int(request["repeats"]),
            workers=int(request["workers"]),
            cold=bool(request["cold"]),
            output=output,
        )
        result = json.loads(output.read_text(encoding="utf-8"))
    receipt = {
        "schema_version": "1.0",
        "authority": "REMOTE_EXECUTION_ONLY",
        "transport": "PERSISTENT_SSH_RPC",
        "request_id": request_id,
        "repository_commit": commit,
        "protocol_sha256": request["protocol_sha256"],
        "candidate_sha256": request["candidate_sha256"],
        "task": request["task"],
        "context": request["context"],
        "workers": request["workers"],
        "started_at_utc": started.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "result_sha256": sha256_object(result),
        "pid": os.getpid(),
    }
    return {
        "request_id": request_id,
        "state": "SUCCEEDED",
        "result": result,
        "receipt": receipt,
        "receipt_sha256": sha256_object(receipt),
    }


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            response = handle(request)
        except Exception as exc:
            response = {
                "request_id": request.get("request_id"),
                "state": "FAILED",
                "error_class": type(exc).__name__,
                "error": str(exc),
            }
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
