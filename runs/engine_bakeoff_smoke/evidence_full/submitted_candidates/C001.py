from __future__ import annotations


def _available_color(
    node: int,
    upper_bound: int,
    colors: list[int],
    neighbors: list[set[int]],
) -> int | None:
    """Return the first valid color below ``upper_bound`` for ``node``."""
    blocked = {colors[other] for other in neighbors[node]}
    for color in range(upper_bound):
        if color not in blocked:
            return color
    return None


def _two_color_components(
    first: int,
    second: int,
    colors: list[int],
    neighbors: list[set[int]],
) -> list[list[int]]:
    """Build induced Kempe components in a stable traversal order."""
    eligible = {
        node for node, color in enumerate(colors) if color == first or color == second
    }
    components: list[list[int]] = []
    while eligible:
        start = min(eligible)
        eligible.remove(start)
        component: list[int] = []
        pending = [start]
        while pending:
            node = pending.pop()
            component.append(node)
            adjacent = sorted(neighbors[node] & eligible, reverse=True)
            for other in adjacent:
                eligible.remove(other)
                pending.append(other)
        components.append(sorted(component))
    return components


def _swap_is_valid(
    component: list[int],
    colors: list[int],
    neighbors: list[set[int]],
) -> bool:
    """Check all edges incident to a swapped component before accepting it."""
    return all(
        colors[node] != colors[other]
        for node in component
        for other in neighbors[node]
    )


def _try_kempe_reassignment(
    node: int,
    target_color: int,
    colors: list[int],
    neighbors: list[set[int]],
    pair_budget: list[int],
) -> bool:
    """Try stable lower-color Kempe interchanges followed by reassignment."""
    adjacent = neighbors[node]
    for first in range(target_color):
        for second in range(first + 1, target_color):
            if pair_budget[0] <= 0:
                return False
            pair_budget[0] -= 1
            for component in _two_color_components(
                first, second, colors, neighbors
            ):
                # A disjoint component cannot change which colors block node.
                if adjacent.isdisjoint(component):
                    continue
                for member in component:
                    colors[member] = (
                        second if colors[member] == first else first
                    )
                replacement = _available_color(
                    node, target_color, colors, neighbors
                )
                if replacement is not None and _swap_is_valid(
                    component, colors, neighbors
                ):
                    colors[node] = replacement
                    return True
                for member in component:
                    colors[member] = (
                        second if colors[member] == first else first
                    )
    return False


def _compact_colors(colors: list[int]) -> None:
    """Renumber the used colors densely while preserving their order."""
    mapping = {color: compact for compact, color in enumerate(sorted(set(colors)))}
    for node, color in enumerate(colors):
        colors[node] = mapping[color]


def _eliminate_terminal_colors(
    colors: list[int],
    neighbors: list[set[int]],
) -> None:
    """Apply bounded, deterministic repair to the highest color class."""
    if not colors:
        return

    # These input-derived caps keep post-processing deterministic and bounded.
    round_limit = min(len(colors), 32)
    pair_budget = [min(4096, max(64, 8 * len(colors)))]
    for _ in range(round_limit):
        target_color = max(colors)
        target_nodes = [
            node for node, color in enumerate(colors) if color == target_color
        ]
        made_progress = False
        for node in target_nodes:
            replacement = _available_color(
                node, target_color, colors, neighbors
            )
            if replacement is not None:
                colors[node] = replacement
                made_progress = True
                continue
            if _try_kempe_reassignment(
                node,
                target_color,
                colors,
                neighbors,
                pair_budget,
            ):
                made_progress = True

        if target_color not in colors:
            _compact_colors(colors)
        elif not made_progress:
            break


def solve(
    node_count: int,
    edges: tuple[tuple[int, int], ...],
    seed: int,
) -> list[int]:
    """Degree-ordered greedy coloring with deterministic terminal repair.

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
    _eliminate_terminal_colors(colors, neighbors)
    return colors
