from __future__ import annotations

import onnx


def rewrite(model: onnx.ModelProto) -> onnx.ModelProto:
    rewritten = onnx.ModelProto.FromString(model.SerializeToString())
    aliases = {
        node.output[0]: node.input[0]
        for node in rewritten.graph.node
        if node.op_type == "Identity" and len(node.input) == len(node.output) == 1
    }

    def resolve(name: str) -> str:
        seen: set[str] = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name

    kept = [node for node in rewritten.graph.node if node.op_type != "Identity"]
    for node in kept:
        for index, name in enumerate(node.input):
            node.input[index] = resolve(name)
    for output in rewritten.graph.output:
        output.name = resolve(output.name)
    del rewritten.graph.node[:]
    rewritten.graph.node.extend(kept)
    return rewritten
