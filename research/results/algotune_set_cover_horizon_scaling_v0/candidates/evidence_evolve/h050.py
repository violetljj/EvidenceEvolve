from __future__ import annotations

from pysat.card import CardEnc, EncType
from pysat.formula import CNF
from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    """Reference AlgoTune set-cover solver exposed as the mutable candidate."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        def quotient_residual(subsets: list[list[int]]) -> list[list[int]]:
            # Elements with the same incident subset IDs impose the identical
            # residual constraint. Retain one deterministic representative per
            # signature before routing the residual to the inherited solver.
            element_incidence: dict[int, list[int]] = {}
            for subset_id, subset in enumerate(subsets, start=1):
                for element in subset:
                    incident_subsets = element_incidence.setdefault(element, [])
                    if not incident_subsets or incident_subsets[-1] != subset_id:
                        incident_subsets.append(subset_id)

            signatures = {
                tuple(incident_subsets)
                for incident_subsets in element_incidence.values()
            }
            return [list(signature) for signature in sorted(signatures)]

        def to_sat(residual: list[list[int]], subset_count: int, bound: int) -> CNF:
            cnf = CNF()
            cnf.extend(residual)
            literals = list(range(1, subset_count + 1))
            cnf.extend(
                CardEnc.atmost(
                    lits=literals,
                    bound=bound,
                    encoding=EncType.seqcounter,
                ).clauses
            )
            return cnf

        residual = quotient_residual(problem)
        left, right = 1, len(problem) + 1
        best: list[int] | None = None
        while left < right:
            midpoint = (left + right) // 2
            with SatSolver(
                name="Minicard",
                bootstrap_with=to_sat(residual, len(problem), midpoint),
            ) as sat:
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
