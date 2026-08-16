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
    """Plateau-aware search that alternates new directions and consolidation."""

    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        self.initial_program: Optional[EvolvedProgram] = None
        self.best_seen_score: Optional[float] = None
        self.meaningful_best_score: Optional[float] = None
        self.last_meaningful_iteration = 0
        self.latest_iteration_seen = 0
        self.latest_program_id: Optional[str] = None
        self.last_diverge_iteration: Optional[int] = None
        self.stagnation_count = 0
        self.program_iterations: Dict[str, int] = {}
        self.parent_uses: Dict[str, int] = {}
        self.context_uses: Dict[str, int] = {}
        self.parent_best_gain: Dict[str, float] = {}
        self.label_parent_uses: Dict[str, int] = {}
        self.recent_labels: List[str] = []

        for program in self.programs.values():
            self.program_iterations[program.id] = self._program_iteration(program)
        self._rebuild_state()

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
    def _program_iteration(program: EvolvedProgram) -> int:
        value = program.iteration_found
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        return 0

    @staticmethod
    def _label(program: EvolvedProgram) -> str:
        info = program.parent_info
        if isinstance(info, tuple) and info:
            return info[0] if isinstance(info[0], str) else ""
        return ""

    @staticmethod
    def _tokens(program: EvolvedProgram) -> set:
        if not isinstance(program.solution, str):
            return set()
        text = program.solution.lower()
        for character in "()[]{}:,=+-*/\n\t":
            text = text.replace(character, " ")
        return set(text.split())

    @classmethod
    def _distance(cls, left: EvolvedProgram, right: EvolvedProgram) -> float:
        left_tokens = cls._tokens(left)
        right_tokens = cls._tokens(right)
        if not left_tokens or not right_tokens:
            return 0.5
        return 1.0 - len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    @staticmethod
    def _weighted_pick(
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

    def _rebuild_state(self) -> None:
        """Reconstruct persistent evidence, including after checkpoint loading."""
        self.parent_uses = {}
        self.context_uses = {}
        self.parent_best_gain = {}
        self.label_parent_uses = {}

        records: List[Tuple[int, float, EvolvedProgram]] = []
        for program in self.programs.values():
            iteration = self.program_iterations.get(
                program.id, self._program_iteration(program)
            )
            timestamp = program.timestamp
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                timestamp = 0.0
            records.append((iteration, float(timestamp), program))

            if isinstance(program.parent_id, str) and program.parent_id:
                parent_id = program.parent_id
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                label = self._label(program)
                if label in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    key = parent_id + "\0" + label
                    self.label_parent_uses[key] = self.label_parent_uses.get(key, 0) + 1

                child_score = self._score(program)
                parent_score = self._score(self.get(parent_id))
                if child_score is not None and parent_score is not None:
                    gain = child_score - parent_score
                    self.parent_best_gain[parent_id] = max(
                        gain, self.parent_best_gain.get(parent_id, -math.inf)
                    )

            context_ids = program.other_context_ids
            if isinstance(context_ids, (list, tuple)):
                for context_id in context_ids:
                    if isinstance(context_id, str):
                        self.context_uses[context_id] = (
                            self.context_uses.get(context_id, 0) + 1
                        )

        records.sort(key=lambda item: (item[0], item[1], item[2].id))
        if not records:
            return

        self.latest_iteration_seen = records[-1][0]
        self.latest_program_id = records[-1][2].id
        self.recent_labels = [self._label(item[2]) for item in records[-4:]]

        running_best: Optional[float] = None
        anchor: Optional[float] = None
        anchor_iteration = records[0][0]
        self.last_diverge_iteration = None

        for iteration, _, program in records:
            if self._label(program) == self.DIVERGE_LABEL:
                self.last_diverge_iteration = iteration

            score = self._score(program)
            if score is None or (running_best is not None and score <= running_best):
                continue
            running_best = score
            if anchor is None:
                anchor = score
                anchor_iteration = iteration
                continue

            relative_threshold = abs(anchor) * 0.01 if abs(anchor) > 1e-12 else 0.01
            if score - anchor > min(0.01, relative_threshold):
                anchor = score
                anchor_iteration = iteration

        self.best_seen_score = running_best
        self.meaningful_best_score = anchor
        self.last_meaningful_iteration = anchor_iteration
        self.stagnation_count = max(0, self.latest_iteration_seen - anchor_iteration)

    def add(
        self, program: EvolvedProgram, iteration: Optional[int] = None, **kwargs: Any
    ) -> str:
        """Add a program and update all selection evidence."""
        if iteration == 0 or program.iteration_found == 0:
            self.initial_program = program

        self.programs[program.id] = program
        raw_iteration = iteration if iteration is not None else program.iteration_found
        if isinstance(raw_iteration, (int, float)) and not isinstance(raw_iteration, bool):
            self.program_iterations[program.id] = int(raw_iteration)
        else:
            self.program_iterations[program.id] = self.latest_iteration_seen + 1

        if iteration is not None:
            self.last_iteration = max(self.last_iteration, iteration)

        self._rebuild_state()
        if self.config.db_path:
            self._save_program(program)
        self._update_best_program(program)
        logger.debug("Added program %s to the evolve database", program.id)
        return program.id

    def _parent(self, scored: List[Tuple[EvolvedProgram, float]], diverge: bool) -> EvolvedProgram:
        scored = sorted(scored, key=lambda item: item[1], reverse=True)
        pool_fraction = 0.50 if diverge else 0.30
        pool = scored[: max(5, math.ceil(len(scored) * pool_fraction))]
        low = scored[-1][1]
        span = max(scored[0][1] - low, 1e-12)
        references = [program for program, _ in scored[: min(5, len(scored))]]
        weighted: List[Tuple[EvolvedProgram, float]] = []

        for program, score in pool:
            quality = (score - low) / span
            underused = 1.0 / (1.0 + self.parent_uses.get(program.id, 0))
            gain = max(0.0, self.parent_best_gain.get(program.id, 0.0))
            productivity = min(1.0, gain / 0.01)
            novelty = sum(self._distance(program, other) for other in references) / len(
                references
            )

            if diverge:
                key = program.id + "\0" + self.DIVERGE_LABEL
                label_freshness = 1.0 / (1.0 + self.label_parent_uses.get(key, 0))
                weight = (
                    0.1
                    + 1.0 * quality
                    + 1.5 * novelty
                    + 1.2 * underused
                    + 0.8 * label_freshness
                )
            else:
                weight = (
                    0.1
                    + 2.8 * quality * quality
                    + 0.9 * underused
                    + 0.8 * productivity
                )
            weighted.append((program, weight))

        return self._weighted_pick(weighted)

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[
        Dict[str, EvolvedProgram],
        Dict[str, List[EvolvedProgram]],
    ]:
        """Choose one parent and diverse, minimally reused context."""
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

        latest = self.get(self.latest_program_id) if self.latest_program_id else None
        latest_score = self._score(latest)
        best_score = self.best_seen_score
        stalled = self.stagnation_count >= 8

        if stalled and latest is not None and latest_score is not None and best_score is not None:
            refine_key = latest.id + "\0" + self.REFINE_LABEL
            unused_refine = self.label_parent_uses.get(refine_key, 0) == 0
            recent_refines = self.recent_labels[-2:].count(self.REFINE_LABEL)
            parent_score = self._score(self.get(latest.parent_id))
            gain = latest_score - parent_score if parent_score is not None else 0.0

            new_direction = self._label(latest) == self.DIVERGE_LABEL
            promising = latest_score >= best_score - 0.04 if new_direction else (
                gain > 0.002 and latest_score >= best_score - 0.015
            )
            if unused_refine and recent_refines < 2 and promising:
                return {self.REFINE_LABEL: latest}, {}

        since_diverge = (
            math.inf
            if self.last_diverge_iteration is None
            else self.latest_iteration_seen - self.last_diverge_iteration
        )
        if stalled and since_diverge >= 3:
            parent = self._parent(scored, diverge=True)
            return {self.DIVERGE_LABEL: parent}, {}

        parent = self._parent(scored, diverge=False)
        try:
            wanted = max(
                0,
                int(num_context_programs if num_context_programs is not None else 4),
            )
        except (TypeError, ValueError):
            wanted = 4

        ranked = sorted(scored, key=lambda item: item[1], reverse=True)
        rank = {program.id: index for index, (program, _) in enumerate(ranked)}
        available = [program for program, _ in ranked if program.id != parent.id]
        targets = [0.02, 0.18, 0.45, 0.75]
        selected: List[EvolvedProgram] = []

        while available and len(selected) < wanted:
            target = targets[len(selected) % len(targets)]
            weighted: List[Tuple[EvolvedProgram, float]] = []
            for program in available:
                position = rank[program.id] / max(1, len(ranked) - 1)
                tier_fit = math.exp(-6.0 * abs(position - target))
                underused = 1.0 / (1.0 + self.context_uses.get(program.id, 0))
                references = [parent] + selected
                novelty = sum(self._distance(program, other) for other in references) / len(
                    references
                )
                same_branch = any(
                    program.parent_id
                    and program.parent_id == other.parent_id
                    for other in references
                )
                branch_factor = 0.5 if same_branch else 1.0
                weight = branch_factor * (
                    0.1 + 1.4 * tier_fit + 1.0 * novelty + 0.9 * underused
                )
                weighted.append((program, weight))

            chosen = self._weighted_pick(weighted)
            selected.append(chosen)
            available = [program for program in available if program.id != chosen.id]

        return {"": parent}, {"": selected}


# EVOLVE-BLOCK-END