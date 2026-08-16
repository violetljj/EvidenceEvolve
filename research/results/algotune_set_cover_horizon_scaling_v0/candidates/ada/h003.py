from __future__ import annotations

from pysat.card import ITotalizer
from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    def solve(self, problem: list[list[int]]) -> list[int]:
        """Encode coverage once and binary-search exact cardinality via totalizer assumptions."""
        count = len(problem)
        if not count:
            return []

        owners: dict[int, list[int]] = {}
        for index, subset in enumerate(problem, 1):
            for element in subset:
                owners.setdefault(element, []).append(index)

        literals = list(range(1, count + 1))
        with ITotalizer(lits=literals, ubound=count, top_id=count) as totalizer:
            clauses = list(owners.values())
            clauses.extend(totalizer.cnf.clauses)
            with SatSolver(name="Minicard", bootstrap_with=clauses) as sat:
                lower, upper = 0, count
                while lower < upper:
                    midpoint = (lower + upper) // 2
                    if sat.solve(assumptions=[-totalizer.rhs[midpoint]]):
                        upper = midpoint
                    else:
                        lower = midpoint + 1

                assumptions = [] if lower == count else [-totalizer.rhs[lower]]
                sat.solve(assumptions=assumptions)
                selected = set(sat.get_model())
        return [index for index in literals if index in selected]
# EVOLVE-BLOCK-END
