from __future__ import annotations

from pysat.solvers import Solver as SatSolver


# EVOLVE-BLOCK-START
class Solver:
    def solve(self, problem: list[list[int]]) -> list[int]:
        """Kernelize masks, certify small covers, then exactly minimize with MiniCard."""
        positions: dict[int, int] = {}
        rows = []
        for row in problem:
            mask = 0
            for element in row:
                position = positions.get(element)
                if position is None:
                    position = len(positions)
                    positions[element] = position
                mask |= 1 << position
            rows.append(mask)
        pending = 0
        for row in rows:
            pending |= row
        if not pending:
            return []
        for index, row in enumerate(rows, 1):
            if row == pending:
                return [index]

        chosen = []
        while pending:
            representatives = {}
            for index, row in enumerate(rows, 1):
                cover = row & pending
                if cover:
                    representatives.setdefault(cover, index)
            kept = []
            for cover, index in sorted(representatives.items(), key=lambda item: item[0].bit_count(), reverse=True):
                if not any(cover | larger == larger for larger, _ in kept):
                    kept.append((cover, index))
            owners = [[] for _ in range(len(positions))]
            for literal, (cover, _) in enumerate(kept, 1):
                remaining = cover
                while remaining:
                    bit = remaining & -remaining
                    owners[bit.bit_length() - 1].append(literal)
                    remaining ^= bit
            forced = {kept[choices[0] - 1][1] for choices in owners if len(choices) == 1}
            if not forced:
                break
            chosen.extend(forced)
            for cover, index in kept:
                if index in forced:
                    pending &= ~cover
        if not pending:
            return sorted(chosen)
        for cover, index in kept:
            if cover == pending:
                return sorted(chosen + [index])

        pending_positions = []
        remaining = pending
        while remaining:
            bit = remaining & -remaining
            pending_positions.append(bit.bit_length() - 1)
            remaining ^= bit
        count = len(kept)
        uncovered = pending
        gain = [cover.bit_count() for cover, _ in kept]
        picked = []
        while uncovered:
            best = max(range(count), key=gain.__getitem__)
            picked.append(best + 1)
            covered = kept[best][0] & uncovered
            while covered:
                bit = covered & -covered
                for literal in owners[bit.bit_length() - 1]:
                    gain[literal - 1] -= 1
                covered ^= bit
            uncovered &= ~kept[best][0]

        def shrink(selection: list[int]) -> list[int]:
            """Discard redundant sets using per-element selected-cover counts."""
            coverage = [0] * len(positions)
            for literal in selection:
                remaining = kept[literal - 1][0]
                while remaining:
                    bit = remaining & -remaining
                    coverage[bit.bit_length() - 1] += 1
                    remaining ^= bit
            active = [True] * len(selection)
            for position in range(len(selection) - 1, -1, -1):
                cover = kept[selection[position] - 1][0]
                remaining = cover
                redundant = True
                while remaining:
                    bit = remaining & -remaining
                    if coverage[bit.bit_length() - 1] == 1:
                        redundant = False
                        break
                    remaining ^= bit
                if redundant:
                    active[position] = False
                    remaining = cover
                    while remaining:
                        bit = remaining & -remaining
                        coverage[bit.bit_length() - 1] -= 1
                        remaining ^= bit
            return [literal for position, literal in enumerate(selection) if active[position]]

        picked = shrink(picked)
        upper = len(picked)
        if upper == 2:
            return sorted(chosen + [kept[literal - 1][1] for literal in picked])
        if upper == 3 and count <= 512:
            masks = [cover for cover, _ in kept]
            for left in range(count):
                left_cover = masks[left]
                for right in range(left + 1, count):
                    if left_cover | masks[right] == pending:
                        return sorted(chosen + [kept[left][1], kept[right][1]])
            return sorted(chosen + [kept[literal - 1][1] for literal in picked])
        if upper == 4 and count <= 96:
            masks = [cover for cover, _ in kept]
            for left in range(count - 2):
                left_cover = masks[left]
                for right in range(left + 1, count - 1):
                    missing = pending & ~(left_cover | masks[right])
                    if not missing:
                        return sorted(chosen + [kept[left][1], kept[right][1]])
                    for third in range(right + 1, count):
                        if masks[third] & missing == missing:
                            return sorted(chosen + [kept[left][1], kept[right][1], kept[third][1]])
            return sorted(chosen + [kept[literal - 1][1] for literal in picked])

        width = max(cover.bit_count() for cover, _ in kept)
        lower = (pending.bit_count() + width - 1) // width
        remaining = pending
        packing = 0
        for position in sorted(pending_positions, key=lambda value: len(owners[value])):
            if remaining & (1 << position):
                for literal in owners[position]:
                    remaining &= ~kept[literal - 1][0]
                packing += 1
        lower = max(lower, packing)
        if lower == upper:
            return sorted(chosen + [kept[literal - 1][1] for literal in picked])

        literals = list(range(1, count + 1))
        clauses = list(dict.fromkeys(tuple(owners[position]) for position in pending_positions))
        if len(clauses) <= 160:
            minimal = []
            for clause in sorted(clauses, key=len):
                options = frozenset(clause)
                if not any(smaller <= options for smaller, _ in minimal):
                    minimal.append((options, clause))
            clauses = [clause for _, clause in minimal]
        with SatSolver(name="Minicard", bootstrap_with=clauses) as sat:
            sat.add_atmost(literals, upper - 1)
            while sat.solve():
                picked = shrink([literal for literal in sat.get_model() if 0 < literal <= count])
                upper = len(picked)
                if upper == lower:
                    break
                sat.add_atmost(literals, upper - 1)
        return sorted(chosen + [kept[literal - 1][1] for literal in picked])
# EVOLVE-BLOCK-END
