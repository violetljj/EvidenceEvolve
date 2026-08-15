from __future__ import annotations

from pysat.card import CardEnc, EncType
from pysat.formula import CNF
from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    """Reference AlgoTune set-cover solver exposed as the mutable candidate."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        def to_sat(subsets: list[list[int]], bound: int) -> CNF:
            universe = sorted({element for subset in subsets for element in subset})
            cnf = CNF()
            for element in universe:
                covers = [
                    index + 1
                    for index, subset in enumerate(subsets)
                    if element in subset
                ]
                cnf.append(covers or [1, -1])
            literals = list(range(1, len(subsets) + 1))
            cnf.extend(
                CardEnc.atmost(
                    lits=literals,
                    bound=bound,
                    encoding=EncType.seqcounter,
                ).clauses
            )
            return cnf

        left, right = 1, len(problem) + 1
        best: list[int] | None = None
        while left < right:
            midpoint = (left + right) // 2
            with SatSolver(name="Minicard", bootstrap_with=to_sat(problem, midpoint)) as sat:
                satisfiable = sat.solve()
                model = sat.get_model() if satisfiable else None
            if model is None:
                left = midpoint + 1
                continue
            selected = [
                index + 1
                for index in range(len(problem))
                if index + 1 in model
            ]
            best = selected
            right = len(selected)
        return best or []
# EVOLVE-BLOCK-END
