from __future__ import annotations

import importlib.util
import random
from pathlib import Path
from time import perf_counter
from types import ModuleType

from evidence_evolve.benchmarks.models import (
    DatasetVisibility,
    GraphInstanceSpec,
    SplitEvaluation,
)
from tasks.graph_coloring.candidates.baseline import solve as baseline_solve


def generate_graph(spec: GraphInstanceSpec) -> tuple[tuple[int, int], ...]:
    generator = random.Random(spec.seed)
    return tuple(
        (left, right)
        for left in range(spec.node_count)
        for right in range(left + 1, spec.node_count)
        if generator.random() < spec.edge_probability
    )


def _load_solver(path: Path) -> ModuleType:
    module_name = f"graph_coloring_candidate_{path.stem}_{path.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import graph-coloring candidate: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "solve", None)):
        raise TypeError("graph-coloring candidate must define solve(node_count, edges, seed)")
    return module


def _validate_coloring(
    colors: object,
    *,
    node_count: int,
    edges: tuple[tuple[int, int], ...],
) -> tuple[bool, int | None, str | None]:
    if not isinstance(colors, list) or len(colors) != node_count:
        return False, None, "COLOR_VECTOR_SHAPE_INVALID"
    if any(isinstance(color, bool) or not isinstance(color, int) or color < 0 for color in colors):
        return False, None, "COLOR_VALUE_INVALID"
    if any(colors[left] == colors[right] for left, right in edges):
        return False, None, "EDGE_CONFLICT"
    return True, len(set(colors)), None


def evaluate_split(
    candidate_path: Path,
    instances: list[GraphInstanceSpec],
    *,
    visibility: DatasetVisibility,
    trial_seed: int,
) -> SplitEvaluation:
    candidate = _load_solver(candidate_path)
    valid_count = 0
    reproducible_count = 0
    baseline_colors: list[int] = []
    candidate_colors: list[int] = []
    relative_improvements: list[float] = []
    failures: list[str] = []
    started = perf_counter()
    for instance in instances:
        edges = generate_graph(instance)
        baseline = baseline_solve(instance.node_count, edges, trial_seed)
        baseline_valid, baseline_count, baseline_error = _validate_coloring(
            baseline, node_count=instance.node_count, edges=edges
        )
        if not baseline_valid or baseline_count is None:
            raise RuntimeError(
                f"frozen graph-coloring baseline invalid on {instance.instance_id}: "
                f"{baseline_error}"
            )
        baseline_colors.append(baseline_count)
        try:
            first = candidate.solve(instance.node_count, edges, trial_seed)
            second = candidate.solve(instance.node_count, edges, trial_seed)
        except Exception as exc:
            failures.append(f"{instance.instance_id}:EXECUTION:{type(exc).__name__}")
            continue
        valid, color_count, error = _validate_coloring(
            first, node_count=instance.node_count, edges=edges
        )
        if not valid or color_count is None:
            failures.append(f"{instance.instance_id}:{error}")
            continue
        valid_count += 1
        second_valid, _, _ = _validate_coloring(
            second, node_count=instance.node_count, edges=edges
        )
        if second_valid and second == first:
            reproducible_count += 1
        else:
            failures.append(f"{instance.instance_id}:NON_REPRODUCIBLE")
        candidate_colors.append(color_count)
        relative_improvements.append((baseline_count - color_count) / baseline_count)
    elapsed = perf_counter() - started
    count = len(instances)
    mean_relative = (
        sum(relative_improvements) / len(relative_improvements)
        if relative_improvements
        else -1.0
    )
    hard_valid = valid_count == count and reproducible_count == count
    return SplitEvaluation(
        split=visibility,
        instance_count=count,
        valid_rate=valid_count / count,
        reproducibility_rate=reproducible_count / count,
        mean_baseline_colors=sum(baseline_colors) / len(baseline_colors),
        mean_candidate_colors=(
            sum(candidate_colors) / len(candidate_colors) if candidate_colors else None
        ),
        mean_relative_improvement=mean_relative,
        positive_relative_improvement=(max(0.0, mean_relative) if hard_valid else 0.0),
        elapsed_seconds=elapsed,
        failure_reasons=failures,
    )
