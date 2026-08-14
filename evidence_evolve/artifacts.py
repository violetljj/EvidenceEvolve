from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

from evidence_evolve.hashing import canonical_json_bytes, sha256_object
from evidence_evolve.models import (
    EnvironmentReceipt,
    EvaluationReceipt,
    ReceiptEnvelope,
)


class ReceiptIntegrityError(RuntimeError):
    pass


def environment_receipt(extra: dict[str, str] | None = None) -> EnvironmentReceipt:
    return EnvironmentReceipt(
        python=sys.version,
        platform=platform.platform(),
        executable=sys.executable,
        extra=extra or {},
    )


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(payload) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_receipt(path: Path, receipt: EvaluationReceipt) -> ReceiptEnvelope:
    envelope = ReceiptEnvelope(
        receipt=receipt,
        receipt_sha256=sha256_object(receipt),
    )
    atomic_write_json(path, envelope)
    return envelope


def load_receipt(path: Path) -> ReceiptEnvelope:
    with path.open("r", encoding="utf-8") as stream:
        envelope = ReceiptEnvelope.model_validate(json.load(stream))
    actual = sha256_object(envelope.receipt)
    if actual != envelope.receipt_sha256:
        raise ReceiptIntegrityError(
            f"receipt hash mismatch: stored={envelope.receipt_sha256} actual={actual}"
        )
    return envelope

