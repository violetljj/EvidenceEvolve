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
    """Randomized near-elite recombination for a short, mature search window."""

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
        self.pair_uses: Dict[Tuple[str, str], int] = {}
        self.labeled_parent_uses: Dict[Tuple[str, str], int] = {}
        self.recent_parent_ids: List[str] = []
        self.fingerprints: Dict[str, set] = {}

        self.breakthrough_id: Optional[str] = None
        self.breakthrough_iteration = -1

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
        for character in "()[]{}.,:=+-*/<>\n\t":
            text = text.replace(character, " ")
        return set(text.split())

    def _distance(self, left: EvolvedProgram, right: EvolvedProgram) -> float:
        left_tokens = self.fingerprints.get(left.id, set())
        right_tokens = self.fingerprints.get(right.id, set())
        if not left_tokens or not right_tokens:
            return 0.5
        union = left_tokens | right_tokens
        return 1.0 - len(left_tokens & right_tokens) / max(1, len(union))

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
        """Store a program and update selection evidence."""
        is_new = program.id not in self.programs
        if iteration == 0 or program.iteration_found == 0:
            self.initial_program = program

        self.programs[program.id] = program
        self.fingerprints[program.id] = self._fingerprint(program)

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

            if current_iteration >= self.latest_iteration:
                if self.breakthrough_iteration < current_iteration:
                    self.breakthrough_id = None
                    self.breakthrough_iteration = -1
                self.latest_iteration = current_iteration

            child_score = self._score(program)
            parent_id = program.parent_id if isinstance(program.parent_id, str) else ""
            parent_score = self._score(self.get(parent_id)) if parent_id else None
            gain: Optional[float] = None

            if parent_id:
                self.parent_uses[parent_id] = self.parent_uses.get(parent_id, 0) + 1
                self.recent_parent_ids.append(parent_id)
                self.recent_parent_ids = self.recent_parent_ids[-5:]
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
                if parent_id:
                    pair = (parent_id, context_id)
                    self.pair_uses[pair] = self.pair_uses.get(pair, 0) + 1
                if gain is not None:
                    self.context_gain_sum[context_id] = (
                        self.context_gain_sum.get(context_id, 0.0) + gain
                    )
                    self.context_gain_count[context_id] = (
                        self.context_gain_count.get(context_id, 0) + 1
                    )

            if isinstance(program.parent_info, tuple) and program.parent_info:
                label = program.parent_info[0]
                target_id = parent_id
                if len(program.parent_info) > 1 and isinstance(
                    program.parent_info[1], str
                ):
                    target_id = program.parent_info[1]
                if label in (self.DIVERGE_LABEL, self.REFINE_LABEL):
                    key = (label, target_id)
                    self.labeled_parent_uses[key] = (
                        self.labeled_parent_uses.get(key, 0) + 1
                    )

            if child_score is not None:
                old_best = self.best_score
                if old_best is None:
                    self.best_score = child_score
                    self.meaningful_best = child_score
                    self.last_meaningful_iteration = current_iteration
                else:
                    self.best_score = max(old_best, child_score)
                    anchor = self.meaningful_best
                    if anchor is None:
                        self.meaningful_best = child_score
                        self.last_meaningful_iteration = current_iteration
                    else:
                        relative = abs(anchor) * 0.01
                        threshold = min(0.01, relative) if relative > 0.0 else 0.01
                        if child_score - anchor > threshold:
                            self.meaningful_best = child_score
                            self.last_meaningful_iteration = current_iteration
                            if child_score > old_best:
                                self.breakthrough_id = program.id
                                self.breakthrough_iteration = current_iteration

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
        """Select an underused near-elite parent and fresh elite contexts."""
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

        # A genuinely meaningful new global best deserves one focused follow-up.
        breakthrough = self.get(self.breakthrough_id) if self.breakthrough_id else None
        if (
            breakthrough is not None
            and self.breakthrough_iteration == self.latest_iteration
            and self.labeled_parent_uses.get(
                (self.REFINE_LABEL, breakthrough.id), 0
            ) == 0
        ):
            return {self.REFINE_LABEL: breakthrough}, {}

        stagnation = max(0, self.latest_iteration - self.last_meaningful_iteration)
        launchpads = [
            item for item in scored if best - 0.007 <= item[1] <= best - 0.0004
        ]
        elite = [item for item in scored if item[1] >= best - 0.004]

        # Most plateau iterations mutate a strong non-maximum launchpad. Every
        # third iteration returns to the absolute elite neighborhood.
        if stagnation >= 8 and len(launchpads) >= 3 and self.latest_iteration % 3:
            parent_pool = launchpads
        else:
            parent_pool = elite or scored[: min(8, len(scored))]

        recent = set(self.recent_parent_ids[-3:])
        fresh_pool = [item for item in parent_pool if item[0].id not in recent]
        if fresh_pool:
            parent_pool = fresh_pool

        parent_weights: List[Tuple[EvolvedProgram, float]] = []
        for program, score in parent_pool:
            quality = max(0.0, 1.0 - (best - score) / 0.008)
            underused = 1.0 / math.sqrt(1.0 + self.parent_uses.get(program.id, 0))
            gain = max(0.0, self.parent_best_gain.get(program.id, 0.0))
            productivity = min(1.0, gain / 0.01)
            weight = 0.2 + quality + 1.4 * underused + 1.5 * productivity
            parent_weights.append((program, weight))
        parent = self._pick(parent_weights)

        try:
            wanted = int(num_context_programs) if num_context_programs is not None else 4
        except (TypeError, ValueError, OverflowError):
            wanted = 4
        wanted = max(0, wanted)

        context_band = 0.018 if stagnation >= 8 else 0.010
        available = [
            (program, score)
            for program, score in scored
            if program.id != parent.id and score >= best - context_band
        ]
        selected: List[EvolvedProgram] = []

        while available and len(selected) < wanted:
            pool = available
            if not selected:
                anchors = [item for item in available if item[1] >= best - 0.001]
                if anchors:
                    pool = anchors

            weighted: List[Tuple[EvolvedProgram, float]] = []
            references = [parent] + selected
            for program, score in pool:
                quality = max(0.0, 1.0 - (best - score) / context_band)
                novelty = sum(
                    self._distance(program, reference) for reference in references
                ) / len(references)
                underused = 1.0 / math.sqrt(
                    1.0 + self.context_uses.get(program.id, 0)
                )
                pair_freshness = 1.0 / math.sqrt(
                    1.0 + self.pair_uses.get((parent.id, program.id), 0)
                )
                count = self.context_gain_count.get(program.id, 0)
                mean_gain = (
                    self.context_gain_sum.get(program.id, 0.0) / count
                    if count
                    else 0.0
                )
                evidence = min(1.0, max(0.0, mean_gain) / 0.005)
                weight = (
                    0.1
                    + 1.4 * quality
                    + 1.5 * novelty
                    + underused
                    + pair_freshness
                    + evidence
                )
                weighted.append((program, weight))

            chosen = self._pick(weighted)
            selected.append(chosen)
            available = [item for item in available if item[0].id != chosen.id]

        return {"": parent}, {"": selected}


# EVOLVE-BLOCK-END