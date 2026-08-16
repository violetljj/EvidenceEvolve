from __future__ import annotations

from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    """Find an exact minimum cover by bisecting MiniCard cardinality bounds."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        """Greedily seed exact bound tightening with warm-started MiniCard."""
        covers: dict[int, list[int]] = {}
        for index, subset in enumerate(problem, 1):
            for element in subset:
                covers.setdefault(element, []).append(index)
        if not covers:
            return []

        gains = [0] * len(problem)
        for options in covers.values():
            for index in options:
                gains[index - 1] += 1
        literals = [index for index, gain in enumerate(gains, 1) if gain]
        remaining = set(covers)
        best: list[int] = []
        while remaining:
            choice = max(range(len(gains)), key=gains.__getitem__)
            best.append(choice + 1)
            for element in problem[choice]:
                if element in remaining:
                    remaining.remove(element)
                    for index in covers[element]:
                        gains[index - 1] -= 1

        with SatSolver(name="Minicard", bootstrap_with=list(covers.values())) as sat:
            sat.start_mode(warm=True)
            while best:
                sat.add_atmost(literals, len(best) - 1)
                if not sat.solve():
                    return best
                best = [literal for literal in sat.get_model() if 0 < literal <= len(problem)]
        return []
# EVOLVE-BLOCK-END
