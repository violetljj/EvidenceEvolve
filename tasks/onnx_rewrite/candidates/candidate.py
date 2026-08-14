from __future__ import annotations

import onnx
from onnx import helper


def rewrite(model: onnx.ModelProto) -> onnx.ModelProto:
    rewritten = onnx.ModelProto.FromString(model.SerializeToString())
    initializers = {item.name for item in rewritten.graph.initializer}
    consumers: dict[str, list[onnx.NodeProto]] = {}
    for node in rewritten.graph.node:
        for name in node.input:
            consumers.setdefault(name, []).append(node)
    fused_add_names: set[str] = set()
    replacement_by_matmul: dict[str, onnx.NodeProto] = {}
    for node in rewritten.graph.node:
        if node.op_type != "MatMul" or len(node.output) != 1:
            continue
        users = consumers.get(node.output[0], [])
        if len(users) != 1 or users[0].op_type != "Add":
            continue
        add = users[0]
        bias_inputs = [name for name in add.input if name != node.output[0]]
        if len(bias_inputs) != 1 or bias_inputs[0] not in initializers:
            continue
        replacement_by_matmul[node.name] = helper.make_node(
            "Gemm",
            [node.input[0], node.input[1], bias_inputs[0]],
            [add.output[0]],
            name=f"{node.name}_bias_fused",
            alpha=1.0,
            beta=1.0,
            transA=0,
            transB=0,
        )
        fused_add_names.add(add.name)
    nodes = []
    for node in rewritten.graph.node:
        if node.name in fused_add_names:
            continue
        nodes.append(replacement_by_matmul.get(node.name, node))
    del rewritten.graph.node[:]
    rewritten.graph.node.extend(nodes)
    return rewritten
