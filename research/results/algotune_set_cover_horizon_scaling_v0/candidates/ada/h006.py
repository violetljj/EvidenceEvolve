from __future__ import annotations

from pysat.card import ITotalizer
from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    def solve(self, problem: list[list[int]]) -> list[int]:
        """Use a greedy cover as an upper bound, then SAT binary-search the exact minimum."""
        count = len(problem)
        rows = [set(subset) for subset in problem]
        owners: dict[int, list[int]] = {}
        for index, row in enumerate(rows, 1):
            for element in row:
                owners.setdefault(element, []).append(index)
        if not owners:
            return []

        uncovered = set(owners)
        gain = [0] + [len(row) for row in rows]
        upper = 0
        while uncovered:
            best = max(range(1, count + 1), key=gain.__getitem__)
            covered = rows[best - 1] & uncovered
            upper += 1
            for element in covered:
                for index in owners[element]:
                    gain[index] -= 1
            uncovered.difference_update(covered)
        if upper == 1:
            return [best]

        literals = list(range(1, count + 1))
        clauses = list(owners.values())
        with ITotalizer(lits=literals, ubound=min(count, upper + 1), top_id=count) as totalizer:
            clauses.extend(totalizer.cnf.clauses)
            with SatSolver(name="Minicard", bootstrap_with=clauses) as sat:
                lower = 1
                while lower < upper:
                    midpoint = (lower + upper) // 2
                    if sat.solve(assumptions=[-totalizer.rhs[midpoint]]):
                        upper = midpoint
                    else:
                        lower = midpoint + 1
                sat.solve(assumptions=[] if lower == count else [-totalizer.rhs[lower]])
                selected = set(sat.get_model())
        return [index for index in literals if index in selected]
# EVOLVE-BLOCK-END
