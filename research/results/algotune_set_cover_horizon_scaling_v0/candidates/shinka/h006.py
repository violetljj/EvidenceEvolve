from __future__ import annotations

from pysat.card import CardEnc, EncType
from pysat.formula import CNF
from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    """Reference AlgoTune set-cover solver exposed as the mutable candidate."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        coverage_by_element: dict[int, list[int]] = {}
        seen: set[frozenset[int]] = set()
        subsets: list[frozenset[int]] = []
        original_indices: list[int] = []
        for original_index, subset in enumerate(problem, start=1):
            elements = frozenset(subset)
            if not elements or elements in seen:
                continue
            seen.add(elements)
            subsets.append(elements)
            original_indices.append(original_index)
            index = len(subsets)
            for element in elements:
                coverage_by_element.setdefault(element, []).append(index)
        coverage_clauses = list(coverage_by_element.values())
        literals = list(range(1, len(subsets) + 1))
        uncovered = set(coverage_by_element)
        available = list(enumerate(subsets, start=1))
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
            selected = [index for index in literals if index in model_set]
            best = selected
            right = len(selected)
        return [original_indices[index - 1] for index in best]
# EVOLVE-BLOCK-END
