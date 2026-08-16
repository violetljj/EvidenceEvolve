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
    """Short-horizon search using elite rotation and brief exploration campaigns."""

    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        self.initial_program: Optional[EvolvedProgram] = None
        self.last_iteration = getattr(self, "last_iteration", 0)
        self.latest_iteration_seen = 0
        self.best_seen_score: Optional[float] = None
        self.meaningful_best_score: Optional[float] = None
        self.last_meaningful_iteration = 0
        self.stagnation_count = 0

        self.parent_uses: Dict[str, int] = {}
        self.context_uses: Dict[str, int] = {}
        self.parent_best_gain: Dict[str, float] = {}

        self.last_diverge_iteration = -1_000_000
        self.active_branch_id: Optional[str] = None
        self.active_branch_score: Optional[float] = None
        self.active_branch_refinements = 0

    @staticmethod
    def _score(program: Optional[EvolvedProgram]) -> Optional[float]:
        if program is None:
            return None
        value = program.metrics.get("combined_score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            score = float(value)
            if math.isfinite(score):
                return score
        return None

    @staticmethod
    def _weighted_choice(
        choices: List[Tuple[EvolvedProgram, float]],
    ) -> EvolvedProgram:
        total = sum(max(0.0, weight) for _, weight in choices)
        if total <= 0.0:
            return random.choice([program for program, _ in choices])

        point = random.random() * total
        for program, weight in choices:
            point -= max(0.0, weight)
            if point <= 0.0:
                return program
        return choices[-1][0]

    def add(
        self, program: EvolvedProgram, iteration: Optional[int] = None, **kwargs: Any
    ) -> str:
        """Store a program and update all persistent selection evidence."""
        is_new = program.id not in self.programs
        parent = self.get(program.parent_id) if program.parent_id else None
        self.programs[program.id] = program

        raw_iteration = iteration if iteration is not None else program.iteration_found
        if (
            isinstance(raw_iteration, (int, float))
            and not isinstance(raw_iteration, bool)
            and math.isfinite(float(raw_iteration))
        ):
            current_iteration = int(raw_iteration)
        else:
            current_iteration = self.latest_iteration_seen + 1

        if current_iteration == 0:
            self.initial_program = program
        self.last_iteration = max(self.last_iteration, current_iteration)

        if is_new:
            self.latest_iteration_seen = max(
                self.latest_iteration_seen, current_iteration
            )
            child_score = self._score(program)
            parent_score = self._score(parent)

            if isinstance(program.parent_id, str) and program.parent_id:
                parent_id = program.parent_id
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                if child_score is not None and parent_score is not None:
                    gain = child_score - parent_score
                    self.parent_best_gain[parent_id] = max(
                        gain, self.parent_best_gain.get(parent_id, -math.inf)
                    )

            for context_id in program.other_context_ids or []:
                if isinstance(context_id, str) and context_id:
                    self.context_uses[context_id] = (
                        self.context_uses.get(context_id, 0) + 1
                    )

            label = ""
            if isinstance(program.parent_info, tuple) and program.parent_info:
                candidate_label = program.parent_info[0]
                if candidate_label in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    label = candidate_label

            meaningful = False
            if child_score is not None:
                if self.best_seen_score is None:
                    self.best_seen_score = child_score
                    self.meaningful_best_score = child_score
                    self.last_meaningful_iteration = current_iteration
                else:
                    self.best_seen_score = max(self.best_seen_score, child_score)
                    anchor = self.meaningful_best_score
                    if anchor is None:
                        meaningful = True
                    else:
                        relative = 0.01 * abs(anchor) if abs(anchor) > 1e-12 else 0.01
                        meaningful = child_score - anchor > min(0.01, relative)
                    if meaningful:
                        self.meaningful_best_score = child_score
                        self.last_meaningful_iteration = current_iteration

            if label == self.DIVERGE_LABEL:
                self.last_diverge_iteration = current_iteration
                best = self.best_seen_score
                if (
                    child_score is not None
                    and best is not None
                    and child_score >= best - 0.05
                ):
                    self.active_branch_id = program.id
                    self.active_branch_score = child_score
                    self.active_branch_refinements = 0
                else:
                    self.active_branch_id = None
                    self.active_branch_score = None

            elif label == self.REFINE_LABEL and self.active_branch_id is not None:
                self.active_branch_refinements += 1
                branch_gain = (
                    child_score - self.active_branch_score
                    if child_score is not None and self.active_branch_score is not None
                    else -math.inf
                )
                if branch_gain > 0.0005 and self.active_branch_refinements < 2:
                    self.active_branch_id = program.id
                    self.active_branch_score = child_score
                else:
                    self.active_branch_id = None
                    self.active_branch_score = None

            if meaningful:
                self.active_branch_id = None
                self.active_branch_score = None

            self.stagnation_count = max(
                0, self.latest_iteration_seen - self.last_meaningful_iteration
            )

        if self.config.db_path:
            self._save_program(program)
        self._update_best_program(program)
        logger.debug("Added program %s to the evolve database", program.id)
        return program.id

    def _choose_parent(
        self,
        scored: List[Tuple[EvolvedProgram, float]],
        diverge: bool = False,
    ) -> EvolvedProgram:
        ranked = sorted(scored, key=lambda item: item[1], reverse=True)
        fraction = 0.50 if diverge else 0.25
        pool_size = min(len(ranked), max(4, math.ceil(len(ranked) * fraction)))
        pool = ranked[:pool_size]

        weighted: List[Tuple[EvolvedProgram, float]] = []
        for rank, (program, _) in enumerate(pool):
            quality = 1.0 - rank / max(1, pool_size)
            underused = 1.0 / math.sqrt(1.0 + self.parent_uses.get(program.id, 0))
            gain = max(0.0, self.parent_best_gain.get(program.id, 0.0))
            productivity = min(1.0, gain / 0.01)

            if diverge:
                weight = 0.2 + quality + 2.0 * underused
            else:
                weight = 0.2 + 2.0 * quality * quality + underused + productivity
            weighted.append((program, weight))

        return self._weighted_choice(weighted)

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[
        Dict[str, EvolvedProgram],
        Dict[str, List[EvolvedProgram]],
    ]:
        """Choose one parent and score-tier-diverse, underused contexts."""
        candidates = list(self.programs.values())
        if not candidates:
            raise ValueError("No candidates available for sampling")

        scored = [
            (program, score)
            for program in candidates
            for score in [self._score(program)]
            if score is not None
        ]

        try:
            wanted = max(
                0,
                int(num_context_programs if num_context_programs is not None else 4),
            )
        except (TypeError, ValueError):
            wanted = 4

        if not scored:
            parent = random.choice(candidates)
            available = [program for program in candidates if program.id != parent.id]
            contexts = random.sample(available, min(wanted, len(available)))
            return {"": parent}, {"": contexts}

        if self.stagnation_count >= 8:
            active = self.get(self.active_branch_id) if self.active_branch_id else None
            if active is not None and self._score(active) is not None:
                return {self.REFINE_LABEL: active}, {}

            if self.latest_iteration_seen - self.last_diverge_iteration >= 3:
                parent = self._choose_parent(scored, diverge=True)
                return {self.DIVERGE_LABEL: parent}, {}

        parent = self._choose_parent(scored)
        ranked = sorted(scored, key=lambda item: item[1])
        rank_fraction = {
            program.id: index / max(1, len(ranked) - 1)
            for index, (program, _) in enumerate(ranked)
        }
        available = [program for program, _ in ranked if program.id != parent.id]
        selected: List[EvolvedProgram] = []
        targets = [1.0, 0.80, 0.50, 0.15]

        while available and len(selected) < wanted:
            target = targets[len(selected) % len(targets)]
            weighted: List[Tuple[EvolvedProgram, float]] = []
            for program in available:
                tier_fit = 1.0 - abs(rank_fraction[program.id] - target)
                underused = 1.0 / (1.0 + self.context_uses.get(program.id, 0))
                references = [parent] + selected
                same_family = any(
                    program.parent_id
                    and program.parent_id == other.parent_id
                    for other in references
                )
                family_factor = 0.45 if same_family else 1.0
                weight = family_factor * (
                    0.1 + 2.0 * tier_fit * tier_fit + underused
                )
                weighted.append((program, weight))

            chosen = self._weighted_choice(weighted)
            selected.append(chosen)
            available = [program for program in available if program.id != chosen.id]

        return {"": parent}, {"": selected}


# EVOLVE-BLOCK-END