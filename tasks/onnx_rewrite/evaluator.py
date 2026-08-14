from __future__ import annotations

import importlib.util
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
import onnx
import onnxruntime as ort

from tasks.onnx_rewrite.model_factory import build_seed_model, input_corpus


DEV_SEED = 1701
CONFIRMATION_SEED = 99173
PARITY_ATOL = 1e-5


def _load_candidate(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("evidence_evolve_candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import candidate: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "rewrite", None)):
        raise TypeError("candidate.py must define rewrite(model)")
    return module


def _session(model: onnx.ModelProto) -> ort.InferenceSession:
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(
        model.SerializeToString(), options, providers=["CPUExecutionProvider"]
    )


def _latency_ms(session: ort.InferenceSession, sample: np.ndarray) -> float:
    for _ in range(10):
        session.run(None, {"input": sample})
    values = []
    for _ in range(50):
        started = time.perf_counter_ns()
        session.run(None, {"input": sample})
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    return float(statistics.median(values))


def evaluate(candidate_path: Path, confirmation: bool = False) -> dict[str, Any]:
    baseline = build_seed_model()
    candidate_module = _load_candidate(candidate_path)
    try:
        rewritten = candidate_module.rewrite(
            onnx.ModelProto.FromString(baseline.SerializeToString())
        )
        if not isinstance(rewritten, onnx.ModelProto):
            raise TypeError("rewrite() did not return onnx.ModelProto")
        onnx.checker.check_model(rewritten, full_check=True)
        checker_failure = 0.0
    except Exception as exc:
        return {
            "mechanics_status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "metrics": {
                "checker_failure": 1.0,
                "max_abs_error": float("inf"),
                "output_shape_mismatch": 1.0,
            },
            "controls": {"onnx_checker": False, "deterministic_rewrite": False, "semantic_parity": False},
        }

    replayed = candidate_module.rewrite(
        onnx.ModelProto.FromString(baseline.SerializeToString())
    )
    deterministic = rewritten.SerializeToString() == replayed.SerializeToString()
    baseline_session = _session(baseline)
    candidate_session = _session(rewritten)
    seed = CONFIRMATION_SEED if confirmation else DEV_SEED
    corpus = input_corpus(seed, 12 if confirmation else 6)
    max_abs = 0.0
    shape_mismatch = 0.0
    for inputs in corpus:
        expected = baseline_session.run(None, {"input": inputs})[0]
        actual = candidate_session.run(None, {"input": inputs})[0]
        if expected.shape != actual.shape:
            shape_mismatch = 1.0
            continue
        max_abs = max(max_abs, float(np.max(np.abs(expected - actual))))

    sample = corpus[-1]
    baseline_nodes = len(baseline.graph.node)
    candidate_nodes = len(rewritten.graph.node)
    baseline_bytes = len(baseline.SerializeToString())
    candidate_bytes = len(rewritten.SerializeToString())
    parity = shape_mismatch == 0.0 and max_abs <= PARITY_ATOL
    return {
        "mechanics_status": "PASS",
        "metrics": {
            "checker_failure": checker_failure,
            "max_abs_error": max_abs,
            "output_shape_mismatch": shape_mismatch,
            "node_count": float(candidate_nodes),
            "node_count_delta": float(candidate_nodes - baseline_nodes),
            "model_bytes": float(candidate_bytes),
            "model_bytes_delta": float(candidate_bytes - baseline_bytes),
            "latency_ms": _latency_ms(candidate_session, sample),
            "baseline_latency_ms": _latency_ms(baseline_session, sample),
        },
        "controls": {
            "onnx_checker": True,
            "deterministic_rewrite": deterministic,
            "semantic_parity": parity,
        },
        "improved": candidate_nodes < baseline_nodes or candidate_bytes < baseline_bytes,
        "seed": seed,
        "confirmation": confirmation,
    }
