from __future__ import annotations

import json
from pathlib import Path
from statistics import fmean, median
from typing import Any

from evidence_evolve.hashing import sha256_file


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "research/parity/engine_selection_r2_effect_first.protocol.json"
UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_engine_selection_upstream/AlgoTuneTasks"


def load_protocol() -> dict[str, Any]:
    payload = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    validate_protocol(payload)
    return payload


def validate_protocol(protocol: dict[str, Any]) -> None:
    if protocol.get("campaign") != "engine_selection_r2_effect_first":
        raise ValueError("Engine Selection R2 campaign drift")
    if tuple(protocol.get("arms", ())) != ("vanilla", "ada", "shinka", "evox"):
        raise ValueError("Engine Selection R2 arm drift")
    if tuple(protocol["round_1"]["repeats"]) != (1,):
        raise ValueError("Engine Selection R2 round-one repeat drift")
    if tuple(protocol["final"]["repeats"]) != (2, 3):
        raise ValueError("Engine Selection R2 final repeat drift")
    conditions = protocol["common_conditions"]
    if conditions["token_policy"] != "ACCOUNT_ONLY_NEVER_STOP_ITERATIONS_NEVER_INVALIDATE_RUN":
        raise ValueError("Engine Selection R2 token policy drift")
    if conditions["token_call_launch_ceiling"] is not None or conditions["token_hard_ceiling"] is not None:
        raise ValueError("Engine Selection R2 cannot contain a token stop")
    tasks = protocol.get("tasks", [])
    if len(tasks) != 3 or len({item["category"] for item in tasks}) != 3:
        raise ValueError("Engine Selection R2 requires three heterogeneous tasks")
    if "job_shop_scheduling" in {item["task"] for item in tasks}:
        raise ValueError("R1-consumed task entered Engine Selection R2")
    for task in tasks:
        source = UPSTREAM_ROOT / task["task"] / f"{task['task']}.py"
        if not source.is_file() or sha256_file(source) != task["source_sha256"]:
            raise ValueError(f"Engine Selection R2 source drift: {task['task']}")
    if protocol["formal_run_count"] != 24:
        raise ValueError("Engine Selection R2 run count drift")
    if protocol["ranking"]["lexicographic_order"][-1] != "lower_total_tokens":
        raise ValueError("tokens must remain the final tie-break")


def _improvement(block: dict[str, Any]) -> float:
    heldout = block["heldout"]
    if not bool(block.get("run_valid")) or not bool(heldout.get("correct")):
        return 0.0
    return max(0.0, float(heldout["raw_speedup"]) - 1.0)


def _aggregate(blocks: list[dict[str, Any]], arms: list[str], repeats: tuple[int, ...]) -> dict[str, Any]:
    protocol = load_protocol()
    tasks = [item["task"] for item in protocol["tasks"]]
    expected = {(task, repeat, arm) for task in tasks for repeat in repeats for arm in arms}
    indexed = {(str(item["task"]), int(item["repeat"]), str(item["arm"])): item for item in blocks}
    if set(indexed) != expected:
        raise ValueError("Engine Selection R2 block coverage drift")
    result: dict[str, Any] = {}
    for arm in arms:
        task_scores: dict[str, float] = {}
        repeat_values: list[float] = []
        for task in tasks:
            values = [_improvement(indexed[(task, repeat, arm)]) for repeat in repeats]
            task_scores[task] = median(values)
            repeat_values.extend(values)
        quality = fmean(task_scores.values())
        mean_wall = fmean(float(indexed[(task, repeat, arm)]["wall_seconds"]) for task in tasks for repeat in repeats)
        total_tokens = sum(int(indexed[(task, repeat, arm)]["observed_tokens"]) for task in tasks for repeat in repeats)
        repeat_mad = fmean(abs(value - quality) for value in repeat_values)
        result[arm] = {
            "task_scores": task_scores,
            "quality_score": quality,
            "median_task_score": median(task_scores.values()),
            "minimum_task_score": min(task_scores.values()),
            "positive_task_count": sum(value > 0.0 for value in task_scores.values()),
            "cross_repeat_mad": repeat_mad,
            "mean_wall_seconds": mean_wall,
            "total_tokens": total_tokens,
            "search_speed_proxy": quality / mean_wall if mean_wall > 0.0 else 0.0,
        }
    return result


def _compare(left: dict[str, Any], right: dict[str, Any], band: float) -> int:
    for key in ("quality_score", "median_task_score", "minimum_task_score"):
        delta = float(left[key]) - float(right[key])
        if abs(delta) > band:
            return 1 if delta > 0.0 else -1
    for key in ("positive_task_count",):
        if left[key] != right[key]:
            return 1 if left[key] > right[key] else -1
    if left["cross_repeat_mad"] != right["cross_repeat_mad"]:
        return 1 if left["cross_repeat_mad"] < right["cross_repeat_mad"] else -1
    if left["search_speed_proxy"] != right["search_speed_proxy"]:
        return 1 if left["search_speed_proxy"] > right["search_speed_proxy"] else -1
    if left["mean_wall_seconds"] != right["mean_wall_seconds"]:
        return 1 if left["mean_wall_seconds"] < right["mean_wall_seconds"] else -1
    if left["total_tokens"] != right["total_tokens"]:
        return 1 if left["total_tokens"] < right["total_tokens"] else -1
    return 0


def _rank(aggregates: dict[str, Any]) -> list[str]:
    band = float(load_protocol()["ranking"]["quality_equivalence_band"])
    remaining = sorted(aggregates)
    ordered: list[str] = []
    while remaining:
        best = remaining[0]
        for arm in remaining[1:]:
            if _compare(aggregates[arm], aggregates[best], band) > 0:
                best = arm
        ordered.append(best)
        remaining.remove(best)
    return ordered


def score_round_1(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    protocol = load_protocol()
    arms = list(protocol["arms"])
    aggregates = _aggregate(blocks, arms, (1,))
    ranking = _rank(aggregates)
    return {
        "schema_version": "1.0",
        "campaign": protocol["campaign"],
        "stage": "ROUND_1",
        "protocol_sha256": sha256_file(PROTOCOL),
        "aggregates": aggregates,
        "ranking": ranking,
        "finalists": ranking[:2],
        "BEST_QUALITY_ENGINE": "PENDING_FINAL",
        "tokens_are_decision_primary": False,
    }


def score_final(round_1: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    protocol = load_protocol()
    if round_1.get("protocol_sha256") != sha256_file(PROTOCOL):
        raise ValueError("Engine Selection R2 round-one protocol drift")
    finalists = [str(item) for item in round_1["finalists"]]
    if len(finalists) != 2:
        raise ValueError("Engine Selection R2 requires two finalists")
    aggregates = _aggregate(blocks, finalists, (2, 3))
    ranking = _rank(aggregates)
    robust = max(
        finalists,
        key=lambda arm: (
            aggregates[arm]["minimum_task_score"],
            aggregates[arm]["positive_task_count"],
            -aggregates[arm]["cross_repeat_mad"],
        ),
    )
    fastest = min(finalists, key=lambda arm: aggregates[arm]["mean_wall_seconds"])
    return {
        "schema_version": "1.0",
        "campaign": protocol["campaign"],
        "stage": "FINAL",
        "protocol_sha256": sha256_file(PROTOCOL),
        "round_1_result_sha256": None,
        "aggregates": aggregates,
        "ranking": ranking,
        "BEST_QUALITY_ENGINE": ranking[0],
        "MOST_ROBUST_ENGINE": robust,
        "FASTEST_ENGINE": fastest,
        "token_role": protocol["ranking"]["token_role"],
        "claim_scope": protocol["claim_scope"],
    }
