from __future__ import annotations

import onnx
import numpy as np
from onnx import numpy_helper


def rewrite(model: onnx.ModelProto) -> onnx.ModelProto:
    rewritten = onnx.ModelProto.FromString(model.SerializeToString())
    constants = {
        initializer.name: numpy_helper.to_array(initializer)
        for initializer in rewritten.graph.initializer
    }
    aliases: dict[str, str] = {}
    kept = []
    for node in rewritten.graph.node:
        removable_input = None
        if node.op_type in {"Add", "Mul"} and len(node.input) == 2:
            neutral = 0.0 if node.op_type == "Add" else 1.0
            for index, name in enumerate(node.input):
                value = constants.get(name)
                if value is not None and np.all(value == neutral):
                    removable_input = node.input[1 - index]
                    break
        if removable_input is None:
            kept.append(node)
        else:
            aliases[node.output[0]] = removable_input

    def resolve(name: str) -> str:
        while name in aliases:
            name = aliases[name]
        return name

    for node in kept:
        for index, name in enumerate(node.input):
            node.input[index] = resolve(name)
    for output in rewritten.graph.output:
        output.name = resolve(output.name)
    used = {name for node in kept for name in node.input}
    del rewritten.graph.node[:]
    rewritten.graph.node.extend(kept)
    retained = [item for item in rewritten.graph.initializer if item.name in used]
    del rewritten.graph.initializer[:]
    rewritten.graph.initializer.extend(retained)
    return rewritten
