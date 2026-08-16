from __future__ import annotations



from pysat.examples.rc2 import RC2
from pysat.formula import WCNF


# EVOLVE-BLOCK-START
class Solver:
    """Find an exact minimum-cardinality set cover."""

    def solve(self, problem: list[list[int]]) -> list[int]:
        """Deduplicate, discard dominated sets, force mandatory sets, then exactly solve residual MaxSAT."""
        unique: dict[frozenset[int], int] = {}
        for index, subset in enumerate(problem, 1):
            members = frozenset(subset)
            if members and members not in unique:
                unique[members] = index
        if not unique:
            return []

        candidates = list(unique.items())
        # A strict subset is never preferable to a superset at equal unit cost.
        if len(candidates) <= 192:
            candidates.sort(key=lambda item: len(item[0]))
            candidates = [
                item for position, item in enumerate(candidates)
                if not any(item[0] <= other[0] for other in candidates[position + 1:])
            ]

        owners: dict[int, list[int]] = {}
        for position, (members, _) in enumerate(candidates):
            for element in members:
                owners.setdefault(element, []).append(position)
        forced_positions = {options[0] for options in owners.values() if len(options) == 1}
        forced = [candidates[position][1] for position in forced_positions]
        remaining = set(owners)
        for position in forced_positions:
            remaining.difference_update(candidates[position][0])
        if not remaining:
            return sorted(forced)

        active = [
            item for position, item in enumerate(candidates)
            if position not in forced_positions and item[0] & remaining
        ]
        covers = {element: [] for element in remaining}
        for variable, (members, _) in enumerate(active, 1):
            for element in members:
                if element in covers:
                    covers[element].append(variable)

        formula = WCNF()
        for options in covers.values():
            formula.append(options)
        for variable in range(1, len(active) + 1):
            formula.append([-variable], weight=1)
        with RC2(formula, incr=True) as optimizer:
            model = optimizer.compute()
        return sorted(forced + [
            active[literal - 1][1] for literal in model if 0 < literal <= len(active)
        ])
# EVOLVE-BLOCK-END
