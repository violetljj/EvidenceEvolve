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
    """Adaptive elite search with branch and context diversity."""

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
        self.parent_best_gain: Dict[str, float] = {}
        self.parent_successes: Dict[str, int] = {}
        self.recent_labels: List[str] = []

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
        return set(program.solution.lower().replace("(", " ").replace(")", " ").split())

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

    def add(
        self, program: EvolvedProgram, iteration: Optional[int] = None, **kwargs: Any
    ) -> str:
        """Add a program and rebuild persistent search evidence."""
        is_new = program.id not in self.programs
        if iteration == 0 or program.iteration_found == 0:
            self.initial_program = program

        self.programs[program.id] = program

        if iteration is not None:
            self.last_iteration = max(self.last_iteration, iteration)

        if is_new:
            raw_iteration = iteration if iteration is not None else program.iteration_found
            if isinstance(raw_iteration, (int, float)) and not isinstance(raw_iteration, bool):
                current_iteration = int(raw_iteration)
            else:
                current_iteration = self.latest_iteration_seen + 1
            self.latest_iteration_seen = max(self.latest_iteration_seen, current_iteration)

            if isinstance(program.parent_id, str) and program.parent_id:
                parent_id = program.parent_id
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                child_score = self._score(program)
                parent_score = self._score(self.get(parent_id))
                if child_score is not None and parent_score is not None:
                    gain = child_score - parent_score
                    self.parent_best_gain[parent_id] = max(
                        gain, self.parent_best_gain.get(parent_id, -math.inf)
                    )
                    if gain > 0.0:
                        self.parent_successes[parent_id] = (
                            self.parent_successes.get(parent_id, 0) + 1
                        )

            for context_id in program.other_context_ids or []:
                if isinstance(context_id, str):
                    self.context_uses[context_id] = self.context_uses.get(context_id, 0) + 1

            label = ""
            if isinstance(program.parent_info, tuple) and program.parent_info:
                recorded = program.parent_info[0]
                if recorded in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    label = recorded
            self.recent_labels.append(label)
            self.recent_labels = self.recent_labels[-8:]

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
                        relative = abs(anchor) * 0.01 if abs(anchor) > 1e-12 else 0.01
                        if score - anchor > min(0.01, relative):
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
        self, scored: List[Tuple[EvolvedProgram, float]], label: str
    ) -> EvolvedProgram:
        scored.sort(key=lambda item: item[1], reverse=True)
        low, high = scored[-1][1], scored[0][1]
        span = max(high - low, 1e-12)

        if label == self.REFINE_LABEL:
            pool = scored[: max(3, math.ceil(len(scored) * 0.20))]
        elif label == self.DIVERGE_LABEL:
            pool = scored[: max(4, math.ceil(len(scored) * 0.50))]
        else:
            pool = scored[: max(4, math.ceil(len(scored) * 0.35))]

        best_program = scored[0][0]
        weighted: List[Tuple[EvolvedProgram, float]] = []
        for program, score in pool:
            quality = (score - low) / span
            underused = 1.0 / (1.0 + self.parent_uses.get(program.id, 0))
            gain = max(0.0, self.parent_best_gain.get(program.id, 0.0))
            productivity = min(1.0, gain / 0.01)
            successes = min(1.0, self.parent_successes.get(program.id, 0) / 2.0)

            if label == self.REFINE_LABEL:
                weight = 0.1 + 2.8 * quality * quality + productivity + successes
            elif label == self.DIVERGE_LABEL:
                novelty = self._distance(program, best_program)
                weight = 0.1 + quality + 1.3 * underused + 1.2 * novelty
            else:
                weight = (
                    0.1
                    + 2.0 * quality * quality
                    + 0.8 * underused
                    + 0.7 * productivity
                    + 0.3 * successes
                )
            weighted.append((program, weight))
        return self._pick(weighted)

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[
        Dict[str, EvolvedProgram],
        Dict[str, List[EvolvedProgram]],
    ]:
        """Select one elite parent and complementary, underused contexts."""
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
            return {"": random.choice(candidates)}, {"": []}

        label = ""
        recent = self.recent_labels[-4:]
        if self.stagnation_count >= 8:
            phase = self.stagnation_count % 5
            if phase == 0 and self.DIVERGE_LABEL not in recent:
                label = self.DIVERGE_LABEL
            elif phase == 2 and self.REFINE_LABEL not in recent[-2:]:
                label = self.REFINE_LABEL

        parent = self._select_parent(scored, label)
        if label:
            return {label: parent}, {}

        try:
            wanted = max(
                0,
                int(num_context_programs if num_context_programs is not None else 4),
            )
        except (TypeError, ValueError):
            wanted = 4

        available = [program for program, _ in scored if program.id != parent.id]
        score_map = {program.id: score for program, score in scored}
        ordered_scores = sorted(score_map.values())
        low, high = ordered_scores[0], ordered_scores[-1]
        span = max(high - low, 1e-12)
        targets = [1.0, 0.8, 0.55, 0.2]
        selected: List[EvolvedProgram] = []

        while available and len(selected) < wanted:
            target = targets[len(selected) % len(targets)]
            weighted: List[Tuple[EvolvedProgram, float]] = []
            for program in available:
                quality = (score_map[program.id] - low) / span
                tier_fit = 1.0 - abs(quality - target)
                underused = 1.0 / (1.0 + self.context_uses.get(program.id, 0))
                references = [parent] + selected
                novelty = sum(self._distance(program, other) for other in references) / len(
                    references
                )
                same_family = any(
                    program.parent_id
                    and program.parent_id == other.parent_id
                    for other in references
                )
                family_factor = 0.55 if same_family else 1.0
                weight = family_factor * (
                    0.1 + 1.2 * tier_fit + 0.9 * novelty + 0.7 * underused
                )
                weighted.append((program, weight))

            chosen = self._pick(weighted)
            selected.append(chosen)
            available = [program for program in available if program.id != chosen.id]

        return {"": parent}, {"": selected}


# EVOLVE-BLOCK-END