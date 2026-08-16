# EVOLVE-BLOCK-START
import logging
import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from skydiscover.config import DatabaseConfig
from skydiscover.search.base_database import Program, ProgramDatabase

logger = logging.getLogger(__name__)


@dataclass
class EvolvedProgram(Program):
    """Program for the evolved database."""


class EvolvedProgramDatabase(ProgramDatabase):
    """Plateau-aware search with short, protected exploration branches."""

    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        self.initial_program = None
        self.best_seen_score: Optional[float] = None
        self.meaningful_best_score: Optional[float] = None
        self.last_meaningful_iteration = 0
        self.latest_iteration_seen = 0
        self.stagnation_count = 0

        self.parent_uses: Dict[str, int] = {}
        self.context_uses: Dict[str, int] = {}
        self.parent_context_uses: Dict[Tuple[str, str], int] = {}
        self.parent_best_gain: Dict[str, float] = {}

        self.active_branch_id: Optional[str] = None
        self.active_branch_remaining = 0
        self.last_diverge_iteration = -1000000

    @staticmethod
    def _score(program: Optional[EvolvedProgram]) -> Optional[float]:
        if program is None:
            return None
        value = program.metrics.get("combined_score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value):
                return value
        return None

    @staticmethod
    def _tokens(program: EvolvedProgram) -> set:
        if not isinstance(program.solution, str):
            return set()
        text = program.solution.lower()
        for character in "()[]{}.,:+-*/=<>\n\t":
            text = text.replace(character, " ")
        return set(text.split())

    @classmethod
    def _distance(cls, first: EvolvedProgram, second: EvolvedProgram) -> float:
        left, right = cls._tokens(first), cls._tokens(second)
        if not left or not right:
            return 0.5
        return 1.0 - len(left & right) / max(1, len(left | right))

    @staticmethod
    def _pick(weighted: List[Tuple[EvolvedProgram, float]]) -> EvolvedProgram:
        total = sum(max(0.0, weight) for _, weight in weighted)
        if total <= 0.0:
            return random.choice([program for program, _ in weighted])
        point = random.random() * total
        for program, weight in weighted:
            point -= max(0.0, weight)
            if point <= 0.0:
                return program
        return weighted[-1][0]

    @staticmethod
    def _wanted(value: Optional[int]) -> int:
        try:
            return max(0, int(4 if value is None else value))
        except (TypeError, ValueError):
            return 4

    def add(
        self, program: EvolvedProgram, iteration: Optional[int] = None, **kwargs: Any
    ) -> str:
        """Add a program and update all persistent search evidence."""
        is_new = program.id not in self.programs
        if iteration == 0 or program.iteration_found == 0:
            self.initial_program = program

        self.programs[program.id] = program
        if iteration is not None:
            self.last_iteration = max(self.last_iteration, iteration)

        if is_new:
            raw_iteration = iteration if iteration is not None else program.iteration_found
            if isinstance(raw_iteration, (int, float)) and not isinstance(
                raw_iteration, bool
            ):
                current_iteration = int(raw_iteration)
            else:
                current_iteration = self.latest_iteration_seen + 1
            self.latest_iteration_seen = max(
                self.latest_iteration_seen, current_iteration
            )

            score = self._score(program)
            best_before = self.best_seen_score
            parent_score = None

            if isinstance(program.parent_id, str) and program.parent_id:
                parent_id = program.parent_id
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                parent_score = self._score(self.get(parent_id))
                if score is not None and parent_score is not None:
                    gain = score - parent_score
                    self.parent_best_gain[parent_id] = max(
                        gain, self.parent_best_gain.get(parent_id, -math.inf)
                    )

                for context_id in program.other_context_ids or []:
                    if isinstance(context_id, str):
                        pair = (parent_id, context_id)
                        self.parent_context_uses[pair] = (
                            self.parent_context_uses.get(pair, 0) + 1
                        )

            for context_id in program.other_context_ids or []:
                if isinstance(context_id, str):
                    self.context_uses[context_id] = (
                        self.context_uses.get(context_id, 0) + 1
                    )

            label = ""
            if isinstance(program.parent_info, tuple) and program.parent_info:
                recorded = program.parent_info[0]
                if recorded in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    label = recorded

            # Give a viable divergent idea two immediate development attempts.
            if label == self.DIVERGE_LABEL:
                self.last_diverge_iteration = current_iteration
                viable = score is not None and (
                    best_before is None or score >= best_before - 0.10
                )
                self.active_branch_id = program.id if viable else None
                self.active_branch_remaining = 2 if viable else 0
            elif self.active_branch_remaining > 0:
                follows_branch = program.parent_id == self.active_branch_id
                viable = score is not None and (
                    best_before is None or score >= best_before - 0.10
                )
                if parent_score is not None and score is not None:
                    viable = viable and score >= parent_score - 0.035
                if follows_branch and viable:
                    self.active_branch_id = program.id
                    self.active_branch_remaining -= 1
                    if self.active_branch_remaining == 0:
                        self.active_branch_id = None
                else:
                    self.active_branch_id = None
                    self.active_branch_remaining = 0

            if score is not None:
                if self.best_seen_score is None:
                    self.best_seen_score = score
                    self.meaningful_best_score = score
                    self.last_meaningful_iteration = current_iteration
                elif score > self.best_seen_score:
                    self.best_seen_score = score
                    anchor = self.meaningful_best_score
                    if anchor is None:
                        self.meaningful_best_score = score
                        self.last_meaningful_iteration = current_iteration
                    else:
                        relative_threshold = (
                            abs(anchor) * 0.01 if abs(anchor) > 1e-12 else 0.01
                        )
                        if score - anchor > min(0.01, relative_threshold):
                            self.meaningful_best_score = score
                            self.last_meaningful_iteration = current_iteration

            self.stagnation_count = max(
                0, self.latest_iteration_seen - self.last_meaningful_iteration
            )

        if self.config.db_path:
            self._save_program(program)
        self._update_best_program(program)
        logger.debug("Added program %s to the evolve database", program.id)
        return program.id

    def _select_parent(
        self, scored: List[Tuple[EvolvedProgram, float]], explore: bool
    ) -> EvolvedProgram:
        ranked = sorted(scored, key=lambda item: item[1], reverse=True)
        fraction = 0.45 if explore else 0.25
        pool = ranked[: max(6, math.ceil(len(ranked) * fraction))]
        best_program, best_score = ranked[0]
        low_score = ranked[-1][1]
        span = max(best_score - low_score, 1e-12)
        newest = max(program.iteration_found for program, _ in ranked)

        weighted: List[Tuple[EvolvedProgram, float]] = []
        for program, score in pool:
            quality = (score - low_score) / span
            underused = 1.0 / math.sqrt(1.0 + self.parent_uses.get(program.id, 0))
            novelty = self._distance(program, best_program)
            gain = min(
                1.0, max(0.0, self.parent_best_gain.get(program.id, 0.0)) / 0.01
            )
            recent = 1.0 / (1.0 + max(0, newest - program.iteration_found))

            if explore:
                weight = 0.1 + 0.8 * quality + 1.5 * underused + 1.3 * novelty
            else:
                weight = (
                    0.1
                    + 2.0 * quality * quality
                    + underused
                    + 0.8 * gain
                    + 0.4 * recent
                )
            weighted.append((program, weight))
        return self._pick(weighted)

    def _select_contexts(
        self,
        parent: EvolvedProgram,
        scored: List[Tuple[EvolvedProgram, float]],
        wanted: int,
        mode: str,
    ) -> List[EvolvedProgram]:
        available = [(program, score) for program, score in scored if program.id != parent.id]
        if not available or wanted <= 0:
            return []

        scores = [score for _, score in scored]
        low, high = min(scores), max(scores)
        span = max(high - low, 1e-12)
        targets = {
            "normal": [1.0, 0.82, 0.55, 0.20],
            "diverge": [1.0, 0.60],
            "repair": [1.0, 0.90, 0.75],
        }[mode]
        selected: List[EvolvedProgram] = []

        while available and len(selected) < wanted:
            target = targets[len(selected) % len(targets)]
            weighted: List[Tuple[EvolvedProgram, float]] = []
            for program, score in available:
                quality = (score - low) / span
                tier_fit = 1.0 - abs(quality - target)
                references = [parent] + selected
                novelty = sum(
                    self._distance(program, other) for other in references
                ) / len(references)
                underused = 1.0 / (1.0 + self.context_uses.get(program.id, 0))
                pair_freshness = 1.0 / (
                    1.0 + self.parent_context_uses.get((parent.id, program.id), 0)
                )
                same_family = any(
                    program.parent_id
                    and program.parent_id == other.parent_id
                    for other in references
                )
                family_factor = 0.55 if same_family else 1.0
                novelty_weight = 1.2 if mode == "diverge" else 0.8
                weight = family_factor * (
                    0.1
                    + 1.2 * tier_fit
                    + novelty_weight * novelty
                    + 0.5 * underused
                    + 0.5 * pair_freshness
                )
                weighted.append((program, weight))

            chosen = self._pick(weighted)
            selected.append(chosen)
            available = [item for item in available if item[0].id != chosen.id]
        return selected

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[
        Dict[str, EvolvedProgram],
        Dict[str, List[EvolvedProgram]],
    ]:
        """Select one parent and complementary contexts."""
        candidates = list(self.programs.values())
        if not candidates:
            raise ValueError("No candidates available for sampling")

        wanted = self._wanted(num_context_programs)
        scored = [
            (program, score)
            for program in candidates
            for score in [self._score(program)]
            if score is not None
        ]
        if not scored:
            parent = random.choice(candidates)
            contexts = [program for program in candidates if program.id != parent.id]
            random.shuffle(contexts)
            return {"": parent}, {"": contexts[:wanted]}

        # Protect a new divergent branch instead of discarding it after one result.
        if self.active_branch_remaining > 0 and self.active_branch_id:
            branch = self.get(self.active_branch_id)
            if branch is not None and self._score(branch) is not None:
                label = self.REFINE_LABEL if self.active_branch_remaining == 2 else ""
                contexts = self._select_contexts(
                    branch, scored, min(wanted, 3), "repair"
                )
                return {label: branch}, {"": contexts}

        # Deep plateaus periodically start a new direction from a strong,
        # underused parent. The cooldown leaves room for ordinary recombination.
        ready_to_diverge = (
            self.stagnation_count >= 10
            and self.latest_iteration_seen - self.last_diverge_iteration >= 3
        )
        if ready_to_diverge:
            parent = self._select_parent(scored, explore=True)
            contexts = self._select_contexts(
                parent, scored, min(wanted, 2), "diverge"
            )
            return {self.DIVERGE_LABEL: parent}, {"": contexts}

        parent = self._select_parent(scored, explore=False)
        contexts = self._select_contexts(parent, scored, wanted, "normal")
        return {"": parent}, {"": contexts}


# EVOLVE-BLOCK-END