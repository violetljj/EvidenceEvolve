from __future__ import annotations

import ast
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
from scipy.stats import rankdata


FEATURE_NAMES = (
    "relative_nearness",
    "depth_approach_rate",
    "local_expansion",
    "path_intrusion",
    "depth_expansion_consistency",
    "observation_quality",
)
DEVELOPMENT_SEED = 2026081601
PARENTS = 6
FRAMES_PER_PARENT = 72
REGIONS_PER_FRAME = 20
_PROCESS_LOCK = threading.Lock()
_FORBIDDEN_CALLS = {
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "getattr",
    "globals",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
    "__import__",
}


def _sigmoid(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _make_split(seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Create a deterministic heterogeneous proxy, not real sensor evidence."""

    rng = np.random.default_rng(seed)
    count = PARENTS * FRAMES_PER_PARENT * REGIONS_PER_FRAME
    parent = np.repeat(np.arange(PARENTS), FRAMES_PER_PARENT * REGIONS_PER_FRAME)
    frame = np.repeat(
        np.arange(PARENTS * FRAMES_PER_PARENT), REGIONS_PER_FRAME
    )

    scene_motion = rng.normal(0.0, 0.17, PARENTS * FRAMES_PER_PARENT)[frame]
    latent_near = rng.beta(2.1, 2.4, count)
    latent_approach = np.clip(
        rng.normal(0.20 + 0.44 * latent_near + scene_motion, 0.30, count),
        -0.55,
        1.0,
    )
    latent_intrusion = np.clip(
        rng.beta(1.8, 2.6, count) + 0.12 * latent_near, 0.0, 1.0
    )
    quality = np.clip(rng.beta(5.0, 1.8, count), 0.05, 1.0)

    parent_bias = np.array([-0.09, 0.04, 0.10, -0.03, 0.07, -0.06])[parent]
    depth_rate = np.clip(
        latent_approach
        + parent_bias
        + rng.normal(0.0, 0.07 + 0.22 * (1.0 - quality), count),
        -1.0,
        1.0,
    )
    expansion = np.clip(
        0.70 * np.maximum(latent_approach, 0.0)
        + 0.20 * latent_near
        + rng.normal(0.0, 0.09 + 0.25 * (1.0 - quality), count),
        -0.35,
        1.0,
    )
    nearness = np.clip(
        latent_near + rng.normal(0.0, 0.05 + 0.12 * (1.0 - quality), count),
        0.0,
        1.0,
    )
    intrusion = np.clip(
        latent_intrusion + rng.normal(0.0, 0.06 + 0.08 * (1.0 - quality), count),
        0.0,
        1.0,
    )
    consistency = np.clip(
        1.0
        - np.abs(np.maximum(depth_rate, 0.0) - np.maximum(expansion, 0.0))
        + rng.normal(0.0, 0.05, count),
        0.0,
        1.0,
    )

    latent_logit = (
        -3.0
        + 1.45 * nearness
        + 1.55 * np.maximum(latent_approach, 0.0)
        + 0.90 * latent_intrusion
        + 1.30 * nearness * np.maximum(latent_approach, 0.0)
        + 0.95 * latent_intrusion * np.maximum(latent_approach, 0.0)
        + 0.45 * consistency
        - 0.55 * (1.0 - quality) * np.abs(depth_rate - expansion)
        + np.array([-0.10, 0.08, 0.02, -0.05, 0.11, -0.07])[parent]
        + rng.normal(0.0, 0.10, count)
    )
    truth = _sigmoid(latent_logit)
    features = np.column_stack(
        (nearness, depth_rate, expansion, intrusion, consistency, quality)
    ).astype(np.float64, copy=False)
    return features, truth, parent, frame


def _validate_source(source: str) -> tuple[bool, str, int]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"syntax_error:{exc.msg}", 0

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name != "math" for alias in node.names):
                return False, "only_math_import_is_allowed", 0
        elif isinstance(node, ast.ImportFrom):
            if node.module not in {"__future__", "math"}:
                return False, "only_math_import_is_allowed", 0
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                return False, f"forbidden_call:{node.func.id}", 0
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return False, "dunder_attribute_is_forbidden", 0

    functions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not any(node.name == "compute_risk" for node in functions):
        return False, "compute_risk_missing", 0
    return True, "", sum(1 for _ in ast.walk(tree))


def _load_candidate(path: Path) -> tuple[Callable[[dict[str, float]], float], int]:
    source = path.read_text(encoding="utf-8")
    valid, error, complexity = _validate_source(source)
    if not valid:
        raise ValueError(error)
    module_name = f"_risk_candidate_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError("candidate_import_spec_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    function = getattr(module, "compute_risk", None)
    if not callable(function):
        raise ValueError("compute_risk_not_callable")
    return function, complexity


def _predict(function: Callable[[dict[str, float]], float], features: np.ndarray) -> tuple[np.ndarray, float]:
    rows = [dict(zip(FEATURE_NAMES, row, strict=True)) for row in features]
    started = time.perf_counter_ns()
    first = np.asarray([function(row) for row in rows], dtype=np.float64)
    elapsed_ns = time.perf_counter_ns() - started
    second = np.asarray([function(row) for row in rows[:128]], dtype=np.float64)
    if first.shape != (len(rows),):
        raise ValueError("output_shape_invalid")
    if not np.all(np.isfinite(first)):
        raise ValueError("non_finite_output")
    if np.any(first < 0.0) or np.any(first > 1.0):
        raise ValueError("output_out_of_range")
    if not np.array_equal(first[:128], second):
        raise ValueError("non_deterministic_output")
    return first, elapsed_ns / max(len(rows), 1) / 1000.0


def _binary_f1(truth: np.ndarray, predicted: np.ndarray) -> float:
    tp = float(np.count_nonzero(truth & predicted))
    fp = float(np.count_nonzero(~truth & predicted))
    fn = float(np.count_nonzero(truth & ~predicted))
    denominator = 2.0 * tp + fp + fn
    return 2.0 * tp / denominator if denominator else 1.0


def _macro_f1(truth: np.ndarray, predicted: np.ndarray) -> float:
    return 0.5 * (_binary_f1(truth, predicted) + _binary_f1(~truth, ~predicted))


def _spearman(truth: np.ndarray, predicted: np.ndarray) -> float:
    truth_rank = rankdata(truth, method="average")
    pred_rank = rankdata(predicted, method="average")
    truth_std = float(np.std(truth_rank))
    pred_std = float(np.std(pred_rank))
    if truth_std == 0.0 or pred_std == 0.0:
        return 0.0
    return float(np.corrcoef(truth_rank, pred_rank)[0, 1])


def _pairwise_accuracy(truth: np.ndarray, predicted: np.ndarray, frame: np.ndarray) -> float:
    correct = 0
    total = 0
    for frame_id in np.unique(frame):
        indices = np.flatnonzero(frame == frame_id)
        left, right = np.triu_indices(len(indices), k=1)
        truth_delta = truth[indices[left]] - truth[indices[right]]
        pred_delta = predicted[indices[left]] - predicted[indices[right]]
        keep = truth_delta != 0.0
        correct += int(np.count_nonzero(np.sign(truth_delta[keep]) == np.sign(pred_delta[keep])))
        total += int(np.count_nonzero(keep))
    return correct / total if total else 0.0


def _score_candidate(path: Path, *, seed: int) -> dict[str, object]:
    function, complexity = _load_candidate(path)
    features, truth, parent, frame = _make_split(seed)
    predicted, runtime_us = _predict(function, features)

    truth_approach = truth >= 0.50
    predicted_approach = predicted >= 0.50
    macro_f1 = _macro_f1(truth_approach, predicted_approach)
    spearman = _spearman(truth, predicted)
    pairwise = _pairwise_accuracy(truth, predicted, frame)
    high_truth = truth >= 0.70
    low_truth = truth <= 0.30
    false_clear = float(np.mean(predicted[high_truth] < 0.60)) if np.any(high_truth) else 0.0
    false_block = float(np.mean(predicted[low_truth] > 0.40)) if np.any(low_truth) else 0.0

    parent_spearman = [_spearman(truth[parent == p], predicted[parent == p]) for p in range(PARENTS)]
    parent_floor = min(parent_spearman)
    parent_consistency = max(0.0, 1.0 - float(np.std(parent_spearman)))

    control_features = features.copy()
    rng = np.random.default_rng(seed ^ 0x5A17C0DE)
    for column in range(5):
        control_features[:, column] = control_features[rng.permutation(len(features)), column]
    control_predicted, _ = _predict(function, control_features)
    control_spearman = _spearman(truth, control_predicted)
    negative_control_gap = max(0.0, spearman - control_spearman)

    complexity_penalty = min(0.04, max(0, complexity - 90) / 5000.0)
    combined = (
        0.30 * macro_f1
        + 0.22 * ((spearman + 1.0) / 2.0)
        + 0.14 * pairwise
        + 0.12 * (1.0 - false_clear)
        + 0.06 * (1.0 - false_block)
        + 0.07 * max(0.0, parent_floor)
        + 0.04 * parent_consistency
        + 0.05 * min(1.0, negative_control_gap / 0.35)
        - complexity_penalty
    )

    return {
        "combined_score": float(combined),
        "approach_macro_f1": float(macro_f1),
        "false_clear": false_clear,
        "false_block": false_block,
        "spearman": float(spearman),
        "pairwise_accuracy": float(pairwise),
        "parent_floor_spearman": float(parent_floor),
        "parent_consistency": float(parent_consistency),
        "negative_control_gap": float(negative_control_gap),
        "negative_control_spearman": float(control_spearman),
        "runtime_us_per_region": float(runtime_us),
        "complexity_ast_nodes": complexity,
        "valid": True,
        "synthetic_mechanics_only": True,
        "text_feedback": (
            f"score={combined:.6f} macro_f1={macro_f1:.4f} "
            f"spearman={spearman:.4f} false_clear={false_clear:.4f} "
            f"false_block={false_block:.4f} pairwise={pairwise:.4f} "
            f"parent_floor={parent_floor:.4f} neg_gap={negative_control_gap:.4f} "
            f"complexity={complexity} runtime_us={runtime_us:.3f}"
        ),
    }


def _invalid_metrics(exc: BaseException) -> dict[str, object]:
    return {
        "combined_score": 0.0,
        "approach_macro_f1": 0.0,
        "false_clear": 1.0,
        "false_block": 1.0,
        "spearman": -1.0,
        "pairwise_accuracy": 0.0,
        "parent_floor_spearman": -1.0,
        "parent_consistency": 0.0,
        "negative_control_gap": 0.0,
        "runtime_us_per_region": 0.0,
        "complexity_ast_nodes": 0,
        "valid": False,
        "synthetic_mechanics_only": True,
        "error": f"{type(exc).__name__}:{exc}",
        "text_feedback": f"invalid candidate: {type(exc).__name__}: {exc}",
    }


def score_without_record(program_path: str, *, seed: int) -> dict[str, object]:
    try:
        return _score_candidate(Path(program_path).resolve(), seed=seed)
    except BaseException as exc:
        return _invalid_metrics(exc)


def _append_record(program_path: Path, metrics: dict[str, object], elapsed: float) -> None:
    ledger_value = os.environ.get("EE_SFR_EVAL_LEDGER")
    if not ledger_value:
        return
    ledger = Path(ledger_value).resolve()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    candidates = ledger.parent / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    source = program_path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()

    with _PROCESS_LOCK:
        with ledger.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            evaluation_index = sum(1 for line in handle if line.strip())
            expected_iterations = int(os.environ.get("EE_SFR_EXPECTED_ITERATIONS", "0"))
            if evaluation_index == 0:
                role = "initial_baseline"
                evox_iteration = None
            elif expected_iterations and evaluation_index > expected_iterations:
                role = "final_best_reevaluation"
                evox_iteration = None
            else:
                role = "solution_candidate"
                evox_iteration = evaluation_index
            candidate_copy = candidates / f"evaluation_{evaluation_index:04d}.py"
            if not candidate_copy.exists():
                shutil.copy2(program_path, candidate_copy)
            record = {
                "schema_version": "1.0",
                "evaluation_index": evaluation_index,
                "evox_iteration": evox_iteration,
                "role": role,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "candidate_path": str(candidate_copy),
                "candidate_sha256": digest,
                "evaluation_wall_seconds": elapsed,
                "metrics": metrics,
            }
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def evaluate_and_record(program_path: str) -> dict[str, object]:
    path = Path(program_path).resolve()
    started = time.perf_counter()
    metrics = score_without_record(str(path), seed=DEVELOPMENT_SEED)
    _append_record(path, metrics, time.perf_counter() - started)
    return metrics
