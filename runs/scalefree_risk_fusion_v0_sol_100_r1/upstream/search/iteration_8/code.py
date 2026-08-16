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
    """Short-horizon elite recombination with evidence-based exploration."""

    def __init__(self, name: str, config: DatabaseConfig):
        super().__init__(name, config)
        self.initial_program = None
        self.best_score: Optional[float] = None
        self.meaningful_best: Optional[float] = None
        self.latest_iteration = 0
        self.last_meaningful_iteration = 0

        self.parent_uses: Dict[str, int] = {}
        self.context_uses: Dict[str, int] = {}
        self.parent_best_gain: Dict[str, float] = {}
        self.context_gain_sum: Dict[str, float] = {}
        self.context_gain_count: Dict[str, int] = {}
        self.label_uses: Dict[str, int] = {}
        self.labeled_parent_uses: Dict[Tuple[str, str], int] = {}

        self.recent_parent_ids: List[str] = []
        self.recent_context_ids: List[str] = []
        self.fingerprints: Dict[str, set] = {}
        self.promising_id: Optional[str] = None
        self.promising_iteration = -1

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
    def _fingerprint(program: EvolvedProgram) -> set:
        if not isinstance(program.solution, str):
            return set()
        text = program.solution.lower()
        for mark in "()[]{}.,:=+-*/<>\n\t":
            text = text.replace(mark, " ")
        return set(text.split())

    def _distance(self, first: EvolvedProgram, second: EvolvedProgram) -> float:
        left = self.fingerprints.get(first.id)
        right = self.fingerprints.get(second.id)
        if left is None:
            left = self._fingerprint(first)
        if right is None:
            right = self._fingerprint(second)
        if not left or not right:
            return 0.5
        return 1.0 - len(left & right) / max(1, len(left | right))

    @staticmethod
    def _roulette(weighted: List[Tuple[EvolvedProgram, float]]) -> EvolvedProgram:
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
        """Store a program and update persistent selection evidence."""
        is_new = program.id not in self.programs
        if iteration == 0 or program.iteration_found == 0:
            self.initial_program = program

        self.programs[program.id] = program
        self.fingerprints[program.id] = self._fingerprint(program)

        if isinstance(iteration, (int, float)) and not isinstance(iteration, bool):
            self.last_iteration = max(
                getattr(self, "last_iteration", 0), int(iteration)
            )

        if is_new:
            raw_iteration = iteration
            if not isinstance(raw_iteration, (int, float)) or isinstance(
                raw_iteration, bool
            ):
                raw_iteration = program.iteration_found
            if isinstance(raw_iteration, (int, float)) and not isinstance(
                raw_iteration, bool
            ):
                current_iteration = int(raw_iteration)
            else:
                current_iteration = self.latest_iteration + 1

            is_latest = current_iteration >= self.latest_iteration
            self.latest_iteration = max(self.latest_iteration, current_iteration)
            if is_latest:
                self.promising_id = None
                self.promising_iteration = -1

            child_score = self._score(program)
            parent_id = program.parent_id if isinstance(program.parent_id, str) else ""
            parent_score = self._score(self.get(parent_id)) if parent_id else None
            gain: Optional[float] = None

            if parent_id:
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                self.recent_parent_ids.append(parent_id)
                self.recent_parent_ids = self.recent_parent_ids[-8:]
                if child_score is not None and parent_score is not None:
                    gain = child_score - parent_score
                    self.parent_best_gain[parent_id] = max(
                        gain, self.parent_best_gain.get(parent_id, -math.inf)
                    )

            context_ids = [
                context_id
                for context_id in (program.other_context_ids or [])
                if isinstance(context_id, str)
            ]
            for context_id in context_ids:
                self.context_uses[context_id] = self.context_uses.get(context_id, 0) + 1
                if gain is not None:
                    self.context_gain_sum[context_id] = (
                        self.context_gain_sum.get(context_id, 0.0) + gain
                    )
                    self.context_gain_count[context_id] = (
                        self.context_gain_count.get(context_id, 0) + 1
                    )
            self.recent_context_ids.extend(context_ids)
            self.recent_context_ids = self.recent_context_ids[-12:]

            label = ""
            target_id = parent_id
            if isinstance(program.parent_info, tuple) and program.parent_info:
                recorded_label = program.parent_info[0]
                if recorded_label in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    label = recorded_label
                    if len(program.parent_info) > 1 and isinstance(
                        program.parent_info[1], str
                    ):
                        target_id = program.parent_info[1]
            if label:
                self.label_uses[label] = self.label_uses.get(label, 0) + 1
                key = (label, target_id)
                self.labeled_parent_uses[key] = self.labeled_parent_uses.get(key, 0) + 1

            if is_latest and gain is not None and parent_score is not None:
                promising_gain = max(0.003, abs(parent_score) * 0.005)
                if gain > promising_gain:
                    self.promising_id = program.id
                    self.promising_iteration = current_iteration

            if child_score is not None:
                if self.best_score is None:
                    self.best_score = child_score
                    self.meaningful_best = child_score
                    self.last_meaningful_iteration = current_iteration
                else:
                    self.best_score = max(self.best_score, child_score)
                    anchor = self.meaningful_best
                    if anchor is None:
                        self.meaningful_best = child_score
                        self.last_meaningful_iteration = current_iteration
                    else:
                        relative_threshold = abs(anchor) * 0.01
                        threshold = (
                            min(0.01, relative_threshold)
                            if relative_threshold > 0.0
                            else 0.01
                        )
                        if child_score - anchor > threshold:
                            self.meaningful_best = child_score
                            self.last_meaningful_iteration = current_iteration

        if self.config.db_path:
            self._save_program(program)
        self._update_best_program(program)
        logger.debug("Added program %s to the evolve database", program.id)
        return program.id

    def _choose_parent(
        self, scored: List[Tuple[EvolvedProgram, float]], diverge: bool = False
    ) -> EvolvedProgram:
        scored = sorted(scored, key=lambda item: item[1], reverse=True)
        best_program, best = scored[0]
        band = max(0.002, abs(best) * 0.003)
        pool = [item for item in scored if item[1] >= best - band]
        if len(pool) < min(4, len(scored)):
            pool = scored[: min(4, len(scored))]

        recent = set(self.recent_parent_ids[-3:])
        fresh = [item for item in pool if item[0].id not in recent]
        if len(fresh) >= 3:
            pool = fresh

        weighted: List[Tuple[EvolvedProgram, float]] = []
        for program, score in pool:
            quality = max(0.0, 1.0 - (best - score) / max(band, 1e-12))
            underused = 1.0 / math.sqrt(1.0 + self.parent_uses.get(program.id, 0))
            gain = max(0.0, self.parent_best_gain.get(program.id, 0.0))
            productivity = min(1.0, gain / 0.01)
            if diverge:
                weight = 0.1 + quality + 1.5 * underused + 1.8 * self._distance(
                    program, best_program
                )
            else:
                weight = 0.1 + 2.2 * quality * quality + underused + productivity
            weighted.append((program, weight))
        return self._roulette(weighted)

    def sample(
        self, num_context_programs: Optional[int] = 4, **kwargs
    ) -> Tuple[
        Dict[str, EvolvedProgram],
        Dict[str, List[EvolvedProgram]],
    ]:
        """Select one near-elite parent and complementary strong contexts."""
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

        scored.sort(key=lambda item: item[1], reverse=True)
        best = scored[0][1]
        stagnation = max(0, self.latest_iteration - self.last_meaningful_iteration)

        # Refine only an immediately promising local breakthrough, and only once.
        promising = self.get(self.promising_id) if self.promising_id else None
        promising_score = self._score(promising)
        if (
            promising is not None
            and promising_score is not None
            and self.promising_iteration == self.latest_iteration
            and promising_score >= best - 0.004
            and self.labeled_parent_uses.get(
                (self.REFINE_LABEL, promising.id), 0
            ) == 0
        ):
            return {self.REFINE_LABEL: promising}, {}

        # Permit one divergence probe only when a converged plateau has never tried it.
        near_best = sum(1 for _, score in scored if score >= best - 0.002)
        converged = near_best >= max(4, math.ceil(len(scored) * 0.08))
        if (
            stagnation >= 8
            and converged
            and self.label_uses.get(self.DIVERGE_LABEL, 0) == 0
        ):
            return {self.DIVERGE_LABEL: self._choose_parent(scored, True)}, {}

        parent = self._choose_parent(scored)
        try:
            wanted = int(num_context_programs) if num_context_programs is not None else 4
        except (TypeError, ValueError, OverflowError):
            wanted = 4
        wanted = max(0, wanted)

        context_band = max(0.015, abs(best) * 0.015)
        available = [
            (program, score)
            for program, score in scored
            if program.id != parent.id and score >= best - context_band
        ]
        selected: List[EvolvedProgram] = []
        recent_contexts = set(self.recent_context_ids[-8:])

        while available and len(selected) < wanted:
            # The first context is an elite anchor; later contexts may contribute
            # distinct upper-tier approaches.
            pool = available
            if not selected:
                elite = [item for item in available if item[1] >= best - 0.002]
                if elite:
                    pool = elite

            weighted: List[Tuple[EvolvedProgram, float]] = []
            references = [parent] + selected
            for program, score in pool:
                quality = max(
                    0.0, 1.0 - (best - score) / max(context_band, 1e-12)
                )
                novelty = sum(
                    self._distance(program, other) for other in references
                ) / len(references)
                underused = 1.0 / math.sqrt(
                    1.0 + self.context_uses.get(program.id, 0)
                )
                count = self.context_gain_count.get(program.id, 0)
                mean_gain = (
                    self.context_gain_sum.get(program.id, 0.0) / count
                    if count
                    else 0.0
                )
                evidence = min(1.0, max(0.0, mean_gain) / 0.005)
                recent_factor = 0.55 if program.id in recent_contexts else 1.0
                sibling_factor = 0.6 if any(
                    program.parent_id
                    and program.parent_id == other.parent_id
                    for other in references
                ) else 1.0
                weight = recent_factor * sibling_factor * (
                    0.1
                    + 1.5 * quality
                    + 1.5 * novelty
                    + 0.9 * underused
                    + evidence
                )
                weighted.append((program, weight))

            chosen = self._roulette(weighted)
            selected.append(chosen)
            available = [item for item in available if item[0].id != chosen.id]

        return {"": parent}, {"": selected}


# EVOLVE-BLOCK-END