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
        uncovered = set(coverage_by_element)
        forced = {
            choices[0]
            for choices in coverage_by_element.values()
            if len(choices) == 1
        }
        if forced:
            covered_by_forced: set[int] = set()
            for index in forced:
                covered_by_forced.update(subsets[index - 1])
            uncovered.difference_update(covered_by_forced)
        # In the residual instance, a strict subset is dominated by a
        # non-forced superset because every chosen subset has unit cost.
        residual_sets = [subset & uncovered for subset in subsets]
        dominated: set[int] = set()
        for index, elements in enumerate(residual_sets, start=1):
            if index in forced or not elements:
                continue
            rarest_element = min(
                elements,
                key=lambda element: len(coverage_by_element[element]),
            )
            for candidate in coverage_by_element[rarest_element]:
                if (
                    candidate != index
                    and candidate not in forced
                    and elements < residual_sets[candidate - 1]
                ):
                    dominated.add(index)
                    break
        coverage_clauses = [
            [
                index
                for index in coverage_by_element[element]
                if index not in forced and index not in dominated
            ]
            for element in uncovered
        ]
        universe_size = len(uncovered)
        from heapq import heapify, heappop, heappush
        gains = [len(elements) for elements in residual_sets]
        max_residual_gain = max(gains, default=1)
        active = [
            gain > 0 and index not in forced and index not in dominated
            for index, gain in enumerate(gains, start=1)
        ]
        literals = [
            index for index, is_active in enumerate(active, start=1) if is_active
        ]
        heap = [(-gains[index - 1], index) for index in literals]
        heapify(heap)
        best: list[int] = []
        if not uncovered:
            return [original_indices[index - 1] for index in sorted(forced)]
        while uncovered:
            while heap:
                negative_gain, index = heappop(heap)
                if active[index - 1] and -negative_gain == gains[index - 1]:
                    break
            else:
                return []
            active[index - 1] = False
            best.append(index)
            for element in subsets[index - 1]:
                if element not in uncovered:
                    continue
                uncovered.remove(element)
                for covered_index in coverage_by_element[element]:
                    if active[covered_index - 1]:
                        gains[covered_index - 1] -= 1
                        heappush(heap, (-gains[covered_index - 1], covered_index))
        left = max(
            1,
            (universe_size + max_residual_gain - 1) // max_residual_gain,
        )
        right = len(best)
        from pysat.card import ITotalizer
        totalizer = ITotalizer(lits=literals, ubound=right)
        with SatSolver(
            name="Minicard",
            bootstrap_with=coverage_clauses + totalizer.cnf.clauses,
        ) as sat:
            while left < right:
                midpoint = (left + right) // 2
                satisfiable = sat.solve(assumptions=[-totalizer.rhs[midpoint]])
                if not satisfiable:
                    left = midpoint + 1
                    continue
                model_set = set(sat.get_model())
                selected = [index for index in literals if index in model_set]
                best = selected
                right = len(selected)
        return [
            original_indices[index - 1]
            for index in sorted(forced | set(best))
        ]
# EVOLVE-BLOCK-END
