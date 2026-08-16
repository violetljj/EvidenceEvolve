from __future__ import annotations

from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    """Reference AlgoTune set-cover solver exposed as the mutable candidate."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        """Encode coverage once and bisect MiniCard's exact at-most bound."""
        covers: dict[int, list[int]] = {}
        for index, subset in enumerate(problem, 1):
            for element in subset:
                covers.setdefault(element, []).append(index)
        if not covers:
            return []

        literals = list(range(1, len(problem) + 1))
        clauses = list(covers.values())
        left, right = 0, len(literals)
        best = literals
        while left < right:
            bound = (left + right) // 2
            with SatSolver(name="Minicard", bootstrap_with=clauses) as sat:
                sat.add_atmost(literals, bound)
                model = sat.get_model() if sat.solve() else None
            if model is None:
                left = bound + 1
                continue
            positive = set(model)
            best = [literal for literal in literals if literal in positive]
            right = len(best)
        return best
# EVOLVE-BLOCK-END
