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
    """Rotating elite search with occasional clean-room exploration."""

    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        self.initial_program: Optional[EvolvedProgram] = None
        self.best_seen_score: Optional[float] = None
        self.meaningful_best_score: Optional[float] = None
        self.last_meaningful_iteration = 0
        self.latest_iteration_seen = 0
        self.stagnation_count = 0
        self.parent_uses: Dict[str, int] = {}
        self.context_uses: Dict[str, int] = {}
        self.innovation_gain: Dict[str, float] = {}
        self.recent_program_ids: List[str] = []

    @staticmethod
    def _score(program: Optional[EvolvedProgram]) -> Optional[float]:
        if program is None or not isinstance(program.metrics, dict):
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
    def _distance(cls, left: EvolvedProgram, right: EvolvedProgram) -> float:
        left_tokens = cls._tokens(left)
        right_tokens = cls._tokens(right)
        if not left_tokens or not right_tokens:
            return 0.5
        return 1.0 - len(left_tokens & right_tokens) / max(
            1, len(left_tokens | right_tokens)
        )

    @staticmethod
    def _weighted_pick(
        choices: List[Tuple[EvolvedProgram, float]],
    ) -> EvolvedProgram:
        weights = [max(0.0, weight) for _, weight in choices]
        total = sum(weights)
        if total <= 0.0:
            return random.choice([program for program, _ in choices])
        point = random.random() * total
        for (program, _), weight in zip(choices, weights):
            point -= weight
            if point <= 0.0:
                return program
        return choices[-1][0]

    def add(
        self, program: EvolvedProgram, iteration: Optional[int] = None, **kwargs: Any
    ) -> str:
        """Add a program and update all persistent selection evidence."""
        is_new = program.id not in self.programs
        self.programs[program.id] = program

        raw_iteration: Any = iteration
        if raw_iteration is None:
            raw_iteration = program.iteration_found
        if isinstance(raw_iteration, (int, float)) and not isinstance(raw_iteration, bool):
            current_iteration = int(raw_iteration)
        else:
            current_iteration = self.latest_iteration_seen

        if current_iteration == 0:
            self.initial_program = program

        if isinstance(iteration, int) and not isinstance(iteration, bool):
            self.last_iteration = max(self.last_iteration, iteration)

        if is_new:
            self.latest_iteration_seen = max(
                self.latest_iteration_seen, current_iteration
            )
            self.recent_program_ids.append(program.id)
            self.recent_program_ids = self.recent_program_ids[-8:]

            if isinstance(program.parent_id, str) and program.parent_id:
                parent_id = program.parent_id
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                child_score = self._score(program)
                parent_score = self._score(self.get(parent_id))
                if child_score is not None and parent_score is not None:
                    self.innovation_gain[program.id] = child_score - parent_score

            context_ids = program.other_context_ids
            if isinstance(context_ids, (list, tuple)):
                for context_id in context_ids:
                    if isinstance(context_id, str):
                        self.context_uses[context_id] = (
                            self.context_uses.get(context_id, 0) + 1
                        )

            score = self._score(program)
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
                        gain = score - anchor
                        if gain > 0.01 or gain > abs(anchor) * 0.01:
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

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[
        Dict[str, EvolvedProgram],
        Dict[str, List[EvolvedProgram]],
    ]:
        """Sample a rotated strong parent and score-tiered diverse context."""
        candidates = list(self.programs.values())
        if not candidates:
            raise ValueError("No candidates available for sampling")

        scored = [
            (program, score)
            for program in candidates
            for score in [self._score(program)]
            if score is not None
        ]
        if not scored:
            parent = random.choice(candidates)
            others = [program for program in candidates if program.id != parent.id]
            random.shuffle(others)
            return {"": parent}, {"": others[:4]}

        scored.sort(key=lambda item: item[1], reverse=True)
        best_program = scored[0][0]

        # Five-step plateau portfolio: three elite steps, one broader bridge
        # step, and one clean-room step. Selection remains stochastic.
        phase = self.latest_iteration_seen % 5 if self.stagnation_count >= 8 else 0
        if phase == 2:
            pool_size = max(10, math.ceil(len(scored) * 0.50))
            mode = "bridge"
        elif phase == 4:
            pool_size = max(8, math.ceil(len(scored) * 0.35))
            mode = "clean"
        else:
            pool_size = max(8, math.ceil(len(scored) * 0.25))
            mode = "elite"
        pool = scored[: min(len(scored), pool_size)]

        parent_choices: List[Tuple[EvolvedProgram, float]] = []
        for rank, (program, _) in enumerate(pool):
            quality = 1.0 - rank / max(1, len(pool) - 1)
            rotation = 1.0 / (1.0 + self.parent_uses.get(program.id, 0))
            novelty = self._distance(program, best_program)
            gain = max(0.0, self.innovation_gain.get(program.id, 0.0))
            momentum = min(1.0, gain / 0.01)

            if mode == "bridge":
                weight = (
                    0.15 + quality + 1.5 * novelty
                    + 1.4 * rotation + 0.8 * momentum
                )
            elif mode == "clean":
                weight = 0.15 + 1.3 * quality + 2.0 * novelty + 1.8 * rotation
            else:
                weight = (
                    0.15 + 2.8 * quality * quality
                    + 1.1 * rotation + 0.8 * momentum
                )
            parent_choices.append((program, weight))

        parent = self._weighted_pick(parent_choices)

        try:
            wanted = max(
                0,
                int(num_context_programs if num_context_programs is not None else 4),
            )
        except (TypeError, ValueError):
            wanted = 4

        # A clean-room generation removes converged context without relying on
        # the historically weak explicit divergence label.
        if mode == "clean" or wanted == 0:
            return {"": parent}, {"": []}
        if mode == "bridge":
            wanted = min(wanted, 2)

        context_pool_size = max(12, math.ceil(len(scored) * 0.75))
        available = [
            program
            for program, _ in scored[: min(len(scored), context_pool_size)]
            if program.id != parent.id
        ]
        rank_map = {program.id: rank for rank, (program, _) in enumerate(scored)}
        targets = [0.0, 0.18, 0.42, 0.68]
        selected: List[EvolvedProgram] = []

        while available and len(selected) < wanted:
            target = targets[len(selected) % len(targets)]
            choices: List[Tuple[EvolvedProgram, float]] = []
            for program in available:
                percentile = rank_map[program.id] / max(1, len(scored) - 1)
                tier_fit = max(0.0, 1.0 - abs(percentile - target) * 2.0)
                rotation = 1.0 / (1.0 + self.context_uses.get(program.id, 0))
                references = [parent] + selected
                novelty = sum(
                    self._distance(program, reference) for reference in references
                ) / len(references)
                same_sibling_group = any(
                    isinstance(program.parent_id, str)
                    and program.parent_id
                    and program.parent_id == reference.parent_id
                    for reference in references
                )
                family_factor = 0.6 if same_sibling_group else 1.0
                weight = family_factor * (
                    0.1 + 1.5 * tier_fit + novelty + 0.9 * rotation
                )
                choices.append((program, weight))

            chosen = self._weighted_pick(choices)
            selected.append(chosen)
            available = [p for p in available if p.id != chosen.id]

        return {"": parent}, {"": selected}


# EVOLVE-BLOCK-END