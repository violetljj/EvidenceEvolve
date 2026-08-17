from __future__ import annotations

import base64

import pytest

from evidence_evolve.hashing import sha256_bytes, sha256_object
from evidence_evolve.remote_eval_rpc import handle


def test_persistent_rpc_rejects_request_hash_drift() -> None:
    candidate = b"def solve():\n    return 1\n"
    request = {
        "schema_version": "1.0",
        "runner_module": "unused",
        "repository_commit": "0" * 40,
        "protocol_sha256": "0" * 64,
        "task": "unused",
        "candidate_base64": base64.b64encode(candidate).decode("ascii"),
        "candidate_sha256": sha256_bytes(candidate),
        "seeds": [1],
        "repeats": 1,
        "workers": 1,
        "cold": False,
        "context": "test",
    }
    request["request_id"] = sha256_object(request)
    request["workers"] = 2

    with pytest.raises(ValueError, match="request hash mismatch"):
        handle(request)
