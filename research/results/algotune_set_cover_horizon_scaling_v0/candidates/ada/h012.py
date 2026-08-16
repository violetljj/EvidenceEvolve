from __future__ import annotations

from pysat.card import ITotalizer
from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    def solve(self, problem: list[list[int]]) -> list[int]:
        """Presolve forced/dominated sets, then exactly SAT-minimize the residual cover."""
        rows = [frozenset(subset) for subset in problem]
        pending: set[int] = set()
        for row in rows:
            pending.update(row)
        if not pending:
            return []

        chosen: list[int] = []
        while pending:
            representatives: dict[frozenset[int], int] = {}
            for index, row in enumerate(rows, 1):
                cover = frozenset(row & pending)
                if cover:
                    representatives.setdefault(cover, index)
            kept: list[tuple[frozenset[int], int]] = []
            for cover, index in sorted(representatives.items(), key=lambda item: len(item[0]), reverse=True):
                if not any(cover <= larger for larger, _ in kept):
                    kept.append((cover, index))

            owners: dict[int, list[int]] = {}
            for literal, (cover, _) in enumerate(kept, 1):
                for element in cover:
                    owners.setdefault(element, []).append(literal)
            forced = {kept[choices[0] - 1][1] for choices in owners.values() if len(choices) == 1}
            if not forced:
                break
            chosen.extend(forced)
            for cover, index in kept:
                if index in forced:
                    pending.difference_update(cover)

        if not pending:
            return sorted(chosen)

        count = len(kept)
        uncovered = set(pending)
        gain = [len(cover) for cover, _ in kept]
        width = max(gain)
        picked: list[int] = []
        upper = 0
        while uncovered:
            best = max(range(count), key=gain.__getitem__)
            covered = kept[best][0] & uncovered
            picked.append(best)
            upper += 1
            for element in covered:
                for literal in owners[element]:
                    gain[literal - 1] -= 1
            uncovered.difference_update(covered)

        lower = (len(pending) + width - 1) // width
        if lower == upper:
            return sorted(chosen + [kept[index][1] for index in picked])

        literals = list(range(1, count + 1))
        clauses = list(dict.fromkeys(tuple(choices) for choices in owners.values()))
        with ITotalizer(lits=literals, ubound=min(count, upper + 1), top_id=count) as totalizer:
            clauses.extend(totalizer.cnf.clauses)
            with SatSolver(name="Minicard", bootstrap_with=clauses) as sat:
                while lower < upper:
                    midpoint = (lower + upper) // 2
                    if sat.solve(assumptions=[-totalizer.rhs[midpoint]]):
                        upper = midpoint
                    else:
                        lower = midpoint + 1
                sat.solve(assumptions=[] if lower == count else [-totalizer.rhs[lower]])
                selected = set(sat.get_model())
        return sorted(chosen + [kept[index - 1][1] for index in literals if index in selected])
# EVOLVE-BLOCK-END
