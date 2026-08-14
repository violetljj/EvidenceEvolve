from __future__ import annotations

import onnx


def rewrite(model: onnx.ModelProto) -> onnx.ModelProto:
    """Seed implementation: return an independent, unchanged model."""
    return onnx.ModelProto.FromString(model.SerializeToString())
