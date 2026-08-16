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
        # Repeat residual reductions: each newly forced set can make further
        # subsets dominated or leave additional elements with one choice.
        dominated: set[int] = set()
        while True:
            residual_sets = [subset & uncovered for subset in subsets]
            dominated.clear()
            residual_representatives: dict[frozenset[int], int] = {}
            for index, elements in enumerate(residual_sets, start=1):
                if index in forced or not elements:
                    continue
                if elements in residual_representatives:
                    dominated.add(index)
                else:
                    residual_representatives[elements] = index
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
            residual_forced = {
                choices[0]
                for element in uncovered
                if len(
                    choices := [
                        index
                        for index in coverage_by_element[element]
                        if index not in forced and index not in dominated
                    ]
                ) == 1
            }
            if not residual_forced:
                break
            forced.update(residual_forced)
            covered_by_residual_forced: set[int] = set()
            for index in residual_forced:
                covered_by_residual_forced.update(subsets[index - 1])
            uncovered.difference_update(covered_by_residual_forced)
        residual_elements = tuple(uncovered)
        universe_size = len(residual_elements)
        from heapq import heapify, heappop, heappush
        gains = [len(elements) for elements in residual_sets]
        max_residual_gain = max(gains, default=1)
        rarity_scores = [
            sum(1.0 / len(coverage_by_element[element]) for element in elements)
            for elements in residual_sets
        ]
        active = [
            gain > 0 and index not in forced and index not in dominated
            for index, gain in enumerate(gains, start=1)
        ]
        active_indices = [
            index for index, is_active in enumerate(active, start=1) if is_active
        ]
        var_by_index = {
            index: variable
            for variable, index in enumerate(active_indices, start=1)
        }
        coverage_clauses = [
            [
                var_by_index[index]
                for index in coverage_by_element[element]
                if index in var_by_index
            ]
            for element in residual_elements
        ]
        literals = list(range(1, len(active_indices) + 1))
        heap = [
            (-gains[index - 1], -rarity_scores[index - 1], index)
            for index in active_indices
        ]
        heapify(heap)
        best: list[int] = []
        if not uncovered:
            return [original_indices[index - 1] for index in sorted(forced)]
        while uncovered:
            while heap:
                negative_gain, _, index = heappop(heap)
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
                        heappush(
                            heap,
                            (
                                -gains[covered_index - 1],
                                -rarity_scores[covered_index - 1],
                                covered_index,
                            ),
                        )
        # Remove greedy selections whose residual elements remain covered by
        # other selected subsets. A smaller feasible cover tightens the SAT
        # upper bound without affecting correctness.
        cover_counts: dict[int, int] = {}
        for index in best:
            for element in residual_sets[index - 1]:
                cover_counts[element] = cover_counts.get(element, 0) + 1
        reduced_best: list[int] = []
        for index in reversed(best):
            elements = residual_sets[index - 1]
            if any(cover_counts[element] == 1 for element in elements):
                reduced_best.append(index)
                continue
            for element in elements:
                cover_counts[element] -= 1
        best = list(reversed(reduced_best))
        # Each packing contains elements that share no eligible subset, so
        # every member requires a distinct selected subset.  Different greedy
        # orders cheaply provide complementary valid lower bounds.
        eligible_choices = {
            element: tuple(
                index
                for index in coverage_by_element[element]
                if index in var_by_index
            )
            for element in residual_elements
        }
        choice_spans = {
            element: sum(
                len(residual_sets[index - 1])
                for index in choices
            )
            for element, choices in eligible_choices.items()
        }
        packing_bound = 0
        packing_orders = (
            sorted(
                residual_elements,
                key=lambda element: (
                    len(eligible_choices[element]),
                    choice_spans[element],
                    element,
                ),
            ),
            sorted(
                residual_elements,
                key=lambda element: (
                    choice_spans[element],
                    len(eligible_choices[element]),
                    element,
                ),
            ),
        )
        for order in packing_orders:
            packed = 0
            blocked_indices: set[int] = set()
            for element in order:
                choices = eligible_choices[element]
                if any(index in blocked_indices for index in choices):
                    continue
                packed += 1
                blocked_indices.update(choices)
            packing_bound = max(packing_bound, packed)
        left = max(
            1,
            packing_bound,
            (universe_size + max_residual_gain - 1) // max_residual_gain,
        )
        right = len(best)
        if left >= right:
            return [
                original_indices[index - 1]
                for index in sorted(forced | set(best))
            ]
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
                selected = [
                    active_indices[variable - 1]
                    for variable in literals
                    if variable in model_set
                ]
                best = selected
                right = len(selected)
        return [
            original_indices[index - 1]
            for index in sorted(forced | set(best))
        ]
# EVOLVE-BLOCK-END
