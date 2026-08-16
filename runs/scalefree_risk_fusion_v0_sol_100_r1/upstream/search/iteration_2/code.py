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
    """Score-aware search with controlled exploitation and exploration."""

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
        self.parent_gain_sum: Dict[str, float] = {}
        self.parent_gain_count: Dict[str, int] = {}
        self.recent_labels: List[str] = []

    @staticmethod
    def _score(program: EvolvedProgram) -> Optional[float]:
        value = program.metrics.get("combined_score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value):
                return value
        return None

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

    def add(self, program: EvolvedProgram, iteration: Optional[int] = None, **kwargs: Any) -> str:
        """Add a program and update persistent selection evidence."""
        is_new = program.id not in self.programs
        if iteration == 0 or program.iteration_found == 0:
            self.initial_program = program

        self.programs[program.id] = program

        if iteration is not None:
            self.last_iteration = max(self.last_iteration, iteration)

        if is_new:
            raw_iteration = iteration if iteration is not None else program.iteration_found
            current_iteration = (
                int(raw_iteration)
                if isinstance(raw_iteration, (int, float)) and not isinstance(raw_iteration, bool)
                else self.latest_iteration_seen + 1
            )
            self.latest_iteration_seen = max(self.latest_iteration_seen, current_iteration)

            if program.parent_id:
                parent_id = program.parent_id
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                parent = self.get(parent_id)
                child_score = self._score(program)
                parent_score = self._score(parent) if parent is not None else None
                if child_score is not None and parent_score is not None:
                    self.parent_gain_sum[parent_id] = (
                        self.parent_gain_sum.get(parent_id, 0.0) + child_score - parent_score
                    )
                    self.parent_gain_count[parent_id] = self.parent_gain_count.get(parent_id, 0) + 1

            for context_id in program.other_context_ids or []:
                if isinstance(context_id, str):
                    self.context_uses[context_id] = self.context_uses.get(context_id, 0) + 1

            label = ""
            if isinstance(program.parent_info, tuple) and program.parent_info:
                candidate_label = program.parent_info[0]
                if candidate_label in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    label = candidate_label
            self.recent_labels.append(label)
            self.recent_labels = self.recent_labels[-8:]

            score = self._score(program)
            if score is not None:
                if self.best_seen_score is None:
                    self.best_seen_score = score
                    self.meaningful_best_score = score
                    self.last_meaningful_iteration = current_iteration
                else:
                    self.best_seen_score = max(self.best_seen_score, score)
                    baseline = self.meaningful_best_score
                    if baseline is not None:
                        threshold = max(0.01, abs(baseline) * 0.01)
                        if score - baseline > threshold:
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

    def _select_parent(self, candidates: List[EvolvedProgram], label: str) -> EvolvedProgram:
        scored = [(program, self._score(program)) for program in candidates]
        scored = [(program, score) for program, score in scored if score is not None]
        if not scored:
            return random.choice(candidates)

        scored.sort(key=lambda item: item[1], reverse=True)
        low, high = scored[-1][1], scored[0][1]
        span = max(high - low, 1e-12)
        pool_size = max(3, min(len(scored), math.ceil(len(scored) * 0.4)))
        pool = scored[:pool_size]

        weighted: List[Tuple[EvolvedProgram, float]] = []
        for program, score in pool:
            quality = (score - low) / span
            underused = 1.0 / (1.0 + self.parent_uses.get(program.id, 0))
            count = self.parent_gain_count.get(program.id, 0)
            mean_gain = self.parent_gain_sum.get(program.id, 0.0) / count if count else 0.0
            gain_signal = max(-1.0, min(1.0, mean_gain / 0.01))

            if label == self.DIVERGE_LABEL:
                weight = 0.15 + quality + 1.2 * underused
            elif label == self.REFINE_LABEL:
                weight = 0.15 + 2.2 * quality * quality + max(0.0, gain_signal)
            else:
                weight = 0.15 + 1.8 * quality * quality + 0.55 * underused
                weight += 0.35 * max(0.0, gain_signal)
            weighted.append((program, weight))
        return self._pick(weighted)

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[Dict[str, EvolvedProgram], Dict[str, List[EvolvedProgram]]]:
        """Select one strong, underused parent and complementary contexts."""
        candidates = list(self.programs.values())
        if not candidates:
            raise ValueError("No candidates available for sampling")

        recent = self.recent_labels[-6:]
        labelled_count = sum(bool(label) for label in recent)
        label = ""
        if self.stagnation_count >= 6 and labelled_count < 2:
            if self.DIVERGE_LABEL not in recent[-4:] and random.random() < 0.70:
                label = self.DIVERGE_LABEL
            elif self.REFINE_LABEL not in recent[-3:] and random.random() < 0.50:
                label = self.REFINE_LABEL
        elif self.stagnation_count >= 3 and self.REFINE_LABEL not in recent[-3:]:
            if random.random() < 0.30:
                label = self.REFINE_LABEL

        parent = self._select_parent(candidates, label)
        if label:
            return {label: parent}, {}

        try:
            wanted = max(0, int(num_context_programs if num_context_programs is not None else 4))
        except (TypeError, ValueError):
            wanted = 4

        available = [program for program in candidates if program.id != parent.id]
        selected: List[EvolvedProgram] = []
        parent_score = self._score(parent)

        while available and len(selected) < wanted:
            numeric_scores = [self._score(program) for program in available]
            numeric_scores = [score for score in numeric_scores if score is not None]
            low = min(numeric_scores) if numeric_scores else 0.0
            high = max(numeric_scores) if numeric_scores else 1.0
            span = max(high - low, 1e-12)
            used_families = {program.parent_id for program in selected if program.parent_id}
            weighted = []

            for program in available:
                score = self._score(program)
                quality = (score - low) / span if score is not None else 0.0
                underused = 1.0 / (1.0 + self.context_uses.get(program.id, 0))
                contrast = (
                    min(1.0, abs(score - parent_score) / span)
                    if score is not None and parent_score is not None
                    else 0.0
                )
                family_factor = 0.45 if program.parent_id in used_families else 1.0
                weight = family_factor * (
                    0.10 + 1.6 * quality * quality + 0.65 * underused + 0.30 * contrast
                )
                weighted.append((program, weight))

            chosen = self._pick(weighted)
            selected.append(chosen)
            available = [program for program in available if program.id != chosen.id]

        return {"": parent}, {"": selected}


# EVOLVE-BLOCK-END