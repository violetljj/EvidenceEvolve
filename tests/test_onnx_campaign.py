from pathlib import Path

import pytest


pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")

from tasks.onnx_rewrite.evaluator import evaluate


def test_seed_candidate_is_valid_but_not_improved() -> None:
    candidate = Path("tasks/onnx_rewrite/candidates/candidate.py")
    result = evaluate(candidate)
    assert result["mechanics_status"] == "PASS"
    assert result["controls"] == {
        "onnx_checker": True,
        "deterministic_rewrite": True,
        "semantic_parity": True,
    }
    assert result["improved"] is False
    assert result["metrics"]["max_abs_error"] == 0.0
