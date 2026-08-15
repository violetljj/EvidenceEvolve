from __future__ import annotations


def solve(
    node_count: int,
    edges: tuple[tuple[int, int], ...],
    seed: int,
) -> list[int]:
    """Deterministic degree-ordered greedy baseline.

    ``seed`` is accepted as part of the frozen candidate interface. The baseline
    intentionally ignores it.
    """
    neighbors = [set() for _ in range(node_count)]
    for left, right in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    order = sorted(range(node_count), key=lambda node: (-len(neighbors[node]), node))
    colors = [-1] * node_count
    for node in order:
        blocked = {colors[other] for other in neighbors[node] if colors[other] >= 0}
        color = 0
        while color in blocked:
            color += 1
        colors[node] = color
    return colors
