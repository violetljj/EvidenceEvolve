from __future__ import annotations

from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    """Exact minimum-cardinality set-cover solver."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        """Tighten one persistent MiniCard bound until the next bound is infeasible."""
        covers: dict[int, list[int]] = {}
        for index, subset in enumerate(problem, 1):
            for element in subset:
                covers.setdefault(element, []).append(index)
        if not covers:
            return []

        literals = list(range(1, len(problem) + 1))
        with SatSolver(name="Minicard", bootstrap_with=covers.values()) as sat:
            sat.solve()
            best = [literal for literal in sat.get_model() if 0 < literal <= len(problem)]
            while best:
                sat.add_atmost(literals, len(best) - 1)
                if not sat.solve():
                    break
                best = [literal for literal in sat.get_model() if 0 < literal <= len(problem)]
        return best
# EVOLVE-BLOCK-END
