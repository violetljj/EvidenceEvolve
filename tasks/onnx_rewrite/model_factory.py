from __future__ import annotations

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


MODEL_SEED = 20260815
INPUT_WIDTH = 8
HIDDEN_WIDTH = 12
OUTPUT_WIDTH = 4


def build_seed_model() -> onnx.ModelProto:
    """Build a fixed non-trivial graph containing safe rewrite opportunities."""
    rng = np.random.default_rng(MODEL_SEED)
    initializers = [
        numpy_helper.from_array(
            rng.normal(0, 0.25, (INPUT_WIDTH, HIDDEN_WIDTH)).astype(np.float32),
            "w1",
        ),
        numpy_helper.from_array(
            rng.normal(0, 0.1, (HIDDEN_WIDTH,)).astype(np.float32), "b1"
        ),
        numpy_helper.from_array(np.ones((HIDDEN_WIDTH,), np.float32), "ones"),
        numpy_helper.from_array(np.zeros((HIDDEN_WIDTH,), np.float32), "zeros_h"),
        numpy_helper.from_array(
            rng.normal(0, 0.2, (HIDDEN_WIDTH, OUTPUT_WIDTH)).astype(np.float32),
            "w2",
        ),
        numpy_helper.from_array(
            rng.normal(0, 0.05, (OUTPUT_WIDTH,)).astype(np.float32), "b2"
        ),
        numpy_helper.from_array(np.zeros((OUTPUT_WIDTH,), np.float32), "zeros_o"),
    ]
    nodes = [
        helper.make_node("Identity", ["input"], ["input_id"], name="input_identity"),
        helper.make_node("MatMul", ["input_id", "w1"], ["mm1"], name="matmul_1"),
        helper.make_node("Add", ["mm1", "b1"], ["biased1"], name="bias_1"),
        helper.make_node("Mul", ["biased1", "ones"], ["scaled1"], name="mul_one"),
        helper.make_node("Add", ["scaled1", "zeros_h"], ["shifted1"], name="add_zero_h"),
        helper.make_node("Relu", ["shifted1"], ["relu"], name="relu"),
        helper.make_node("Identity", ["relu"], ["relu_id"], name="middle_identity"),
        helper.make_node("MatMul", ["relu_id", "w2"], ["mm2"], name="matmul_2"),
        helper.make_node("Add", ["mm2", "b2"], ["biased2"], name="bias_2"),
        helper.make_node("Add", ["biased2", "zeros_o"], ["output"], name="add_zero_o"),
    ]
    graph = helper.make_graph(
        nodes,
        "evidence_evolve_onnx_rewrite_r0",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [None, INPUT_WIDTH])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [None, OUTPUT_WIDTH])],
        initializer=initializers,
    )
    model = helper.make_model(
        graph,
        opset_imports=[helper.make_opsetid("", 17)],
        producer_name="EvidenceEvolve",
    )
    model.ir_version = 10
    onnx.checker.check_model(model)
    return model


def input_corpus(seed: int, batches: int) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    sizes = [1, 2, 7, 16]
    return [
        rng.normal(0, 2.0, (sizes[index % len(sizes)], INPUT_WIDTH)).astype(np.float32)
        for index in range(batches)
    ]
