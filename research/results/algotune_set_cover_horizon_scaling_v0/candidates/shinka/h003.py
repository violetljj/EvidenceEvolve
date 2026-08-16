from __future__ import annotations

from pysat.card import CardEnc, EncType
from pysat.formula import CNF
from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    """Reference AlgoTune set-cover solver exposed as the mutable candidate."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        coverage_by_element: dict[int, list[int]] = {}
        for index, subset in enumerate(problem, start=1):
            for element in set(subset):
                coverage_by_element.setdefault(element, []).append(index)
        coverage_clauses = list(coverage_by_element.values())
        literals = list(range(1, len(problem) + 1))
        uncovered = set(coverage_by_element)
        available = [
            (index, set(subset))
            for index, subset in enumerate(problem, start=1)
            if subset
        ]
        best: list[int] = []
        while uncovered:
            choice = max(
                available,
                key=lambda candidate: len(candidate[1] & uncovered),
            )
            available.remove(choice)
            best.append(choice[0])
            uncovered.difference_update(choice[1])
        left, right = 1, len(best)
        while left < right:
            midpoint = (left + right) // 2
            with SatSolver(name="Minicard", bootstrap_with=coverage_clauses) as sat:
                sat.add_atmost(literals, midpoint)
                satisfiable = sat.solve()
                model = sat.get_model() if satisfiable else None
            if model is None:
                left = midpoint + 1
                continue
            model_set = set(model)
            selected = [
                index + 1
                for index in range(len(problem))
                if index + 1 in model_set
            ]
            best = selected
            right = len(selected)
        return best or []
# EVOLVE-BLOCK-END
