"""Thin black-box entry point used by SkyDiscover.

The model receives the problem statement from evox.yaml. Evaluator context
injection is disabled, so metric implementation and synthetic labels are not
included in solution-generation prompts.
"""

from __future__ import annotations

from locked_eval import evaluate_and_record


def evaluate(program_path: str) -> dict[str, object]:
    return evaluate_and_record(program_path)

