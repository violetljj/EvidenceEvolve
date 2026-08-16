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
    """Score-aware search with adaptive exploration during stagnation."""

    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        self.initial_program = None
        self.best_score_seen: Optional[float] = None
        self.last_meaningful_improvement_iteration = 0
        self.score_records: Dict[str, Tuple[int, float]] = {}
        self.recorded_ids: Dict[str, bool] = {}
        self.parent_uses: Dict[str, int] = {}
        self.context_uses: Dict[str, int] = {}
        self.child_scores: Dict[str, List[float]] = {}
        self.label_uses: Dict[str, int] = {
            self.DIVERGE_LABEL: 0,
            self.REFINE_LABEL: 0,
        }
        self.label_parent_uses: Dict[str, int] = {}

    @staticmethod
    def _score(program: EvolvedProgram) -> Optional[float]:
        if not isinstance(program.metrics, dict):
            return None
        value = program.metrics.get("combined_score")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    @staticmethod
    def _choose(items: List[EvolvedProgram], weights: List[float]) -> EvolvedProgram:
        return random.choices(items, weights=[max(0.001, w) for w in weights], k=1)[0]

    def _rebuild_progress(self) -> None:
        best: Optional[float] = None
        last_meaningful = 0
        for iteration, score in sorted(self.score_records.values()):
            if best is None:
                best = score
                last_meaningful = iteration
            elif score > best:
                gain = score - best
                if gain > 0.01 or gain > 0.01 * max(abs(best), 1e-12):
                    last_meaningful = iteration
                best = score
        self.best_score_seen = best
        self.last_meaningful_improvement_iteration = last_meaningful

    def add(self, program: EvolvedProgram, iteration: Optional[int] = None, **kwargs: Any) -> str:
        """Add a program and reconstruct all persistent search statistics."""
        found_iteration = iteration
        if not isinstance(found_iteration, int) or isinstance(found_iteration, bool):
            found_iteration = program.iteration_found
        if not isinstance(found_iteration, int) or isinstance(found_iteration, bool):
            found_iteration = self.last_iteration

        if found_iteration == 0 or program.iteration_found == 0:
            self.initial_program = program

        self.programs[program.id] = program
        self.last_iteration = max(self.last_iteration, found_iteration)

        if program.id not in self.recorded_ids:
            self.recorded_ids[program.id] = True
            score = self._score(program)
            if score is not None:
                self.score_records[program.id] = (found_iteration, score)

            parent_id = program.parent_id
            if isinstance(parent_id, str) and parent_id:
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                if score is not None:
                    self.child_scores.setdefault(parent_id, []).append(score)

            seen_context_ids: Dict[str, bool] = {}
            if isinstance(program.other_context_ids, list):
                for context_id in program.other_context_ids:
                    if isinstance(context_id, str) and context_id and context_id not in seen_context_ids:
                        seen_context_ids[context_id] = True
                        self.context_uses[context_id] = self.context_uses.get(context_id, 0) + 1

            info = program.parent_info
            if isinstance(info, tuple) and len(info) == 2:
                label, labeled_parent_id = info
                if label in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    self.label_uses[label] = self.label_uses.get(label, 0) + 1
                    if isinstance(labeled_parent_id, str):
                        key = label + "\0" + labeled_parent_id
                        self.label_parent_uses[key] = self.label_parent_uses.get(key, 0) + 1

            self._rebuild_progress()

        if self.config.db_path:
            self._save_program(program)
        self._update_best_program(program)

        logger.debug(f"Added program {program.id} to the evolve database")
        return program.id

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[Dict[str, EvolvedProgram], Dict[str, List[EvolvedProgram]]]:
        """Select a productive but underused parent and complementary contexts."""
        all_programs = list(self.programs.values())
        if not all_programs:
            raise ValueError("No candidates available for sampling")

        scored = [(program, self._score(program)) for program in all_programs]
        scored = [(program, score) for program, score in scored if score is not None]
        candidates = [program for program, _ in scored] or all_programs
        score_by_id = {program.id: score for program, score in scored}
        values = list(score_by_id.values())
        low = min(values) if values else 0.0
        high = max(values) if values else 1.0
        spread = max(high - low, 1e-12)
        stagnant = max(0, self.last_iteration - self.last_meaningful_improvement_iteration)
        exploration = min(1.0, stagnant / 8.0)

        def quality(program: EvolvedProgram) -> float:
            score = score_by_id.get(program.id)
            return 0.25 if score is None else (score - low) / spread

        def novelty(program: EvolvedProgram, uses: Dict[str, int]) -> float:
            return 1.0 / ((1.0 + uses.get(program.id, 0)) ** 0.5)

        def outcome(program: EvolvedProgram) -> float:
            children = self.child_scores.get(program.id, [])
            if not children:
                return 0.5
            return max(0.0, min(1.0, (sum(children) / len(children) - low) / spread))

        def fertility(program: EvolvedProgram) -> float:
            children = self.child_scores.get(program.id, [])
            score = score_by_id.get(program.id)
            if not children or score is None:
                return 0.0
            return max(0.0, min(1.0, (max(children) - score) / spread))

        # Labels are reserved for genuine plateaus and are balanced across parents.
        label_probability = min(0.40, 0.12 + 0.03 * stagnant)
        if stagnant >= 4 and len(candidates) >= 4 and random.random() < label_probability:
            diverge_pressure = 0.65 if spread < 0.02 else 0.50
            if self.label_uses[self.DIVERGE_LABEL] > self.label_uses[self.REFINE_LABEL] + 1:
                diverge_pressure = 0.30
            label = (
                self.DIVERGE_LABEL
                if random.random() < diverge_pressure
                else self.REFINE_LABEL
            )
            weights = []
            for program in candidates:
                labeled_uses = self.label_parent_uses.get(label + "\0" + program.id, 0)
                unused = 1.0 / ((1.0 + labeled_uses) ** 0.5)
                if label == self.REFINE_LABEL:
                    weights.append(0.65 * quality(program) + 0.25 * unused + 0.10 * outcome(program))
                else:
                    weights.append(0.45 * quality(program) + 0.35 * unused + 0.20 * (1.0 - outcome(program)))
            parent = self._choose(candidates, weights)
            return {label: parent}, {}

        parent_weights = []
        for program in candidates:
            parent_weights.append(
                (0.55 - 0.25 * exploration) * quality(program)
                + 0.20 * outcome(program)
                + 0.15 * fertility(program)
                + (0.20 + 0.25 * exploration) * novelty(program, self.parent_uses)
            )
        parent = self._choose(candidates, parent_weights)

        count = 4 if num_context_programs is None else max(0, int(num_context_programs))
        pool = [program for program in candidates if program.id != parent.id]
        contexts: List[EvolvedProgram] = []
        parent_score = score_by_id.get(parent.id, high)

        while pool and len(contexts) < count:
            slot = len(contexts)
            weights = []
            for program in pool:
                distance = abs(score_by_id.get(program.id, low) - parent_score) / spread
                unused = novelty(program, self.context_uses)
                if slot == 0:
                    weight = 0.55 * quality(program) + 0.45 * unused
                elif slot == 1:
                    weight = 0.55 * distance + 0.45 * unused
                else:
                    weight = 0.35 * quality(program) + 0.25 * distance + 0.40 * unused
                weights.append(weight)
            selected = self._choose(pool, weights)
            contexts.append(selected)
            pool = [program for program in pool if program.id != selected.id]

        return {"": parent}, {"": contexts}


# EVOLVE-BLOCK-END