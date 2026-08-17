from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path
from statistics import fmean
from typing import Any

from evidence_evolve.benchmarks.algotune_official import load_task
from evidence_evolve.hashing import sha256_file, sha256_object


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "research/parity/engine_selection_r1.protocol.json"
UPSTREAM_ROOT = REPO_ROOT / "tasks/algotune_engine_selection_upstream/AlgoTuneTasks"
M4_PROTOCOL_GLOB = "m4_search_value_tournament_v*.protocol.json"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_path(task_name: str) -> Path:
    return UPSTREAM_ROOT / task_name / f"{task_name}.py"


def _m4_protocols() -> list[Path]:
    return sorted((REPO_ROOT / "research/parity").glob(M4_PROTOCOL_GLOB))


def consumed_m4_tasks() -> set[str]:
    return {
        str(task["task"])
        for path in _m4_protocols()
        for task in _read_json(path)["tasks"]
    }


def load_protocol() -> dict[str, Any]:
    protocol = _read_json(PROTOCOL)
    if protocol.get("campaign") != "engine_selection_r1":
        raise ValueError("Engine Selection campaign drift")
    if tuple(protocol["arms"]) != ("vanilla", "ada", "shinka", "evox"):
        raise ValueError("Engine Selection arm drift")
    if tuple(protocol["core_repeats"]) != (1, 2):
        raise ValueError("Engine Selection core repeat drift")
    if tuple(protocol["reserve_repeats"]) != (1, 2):
        raise ValueError("Engine Selection reserve repeat drift")
    checkpoints = tuple(protocol["common_conditions"]["token_checkpoints"])
    if checkpoints != (50_000, 100_000, 200_000):
        raise ValueError("Engine Selection token checkpoint drift")
    if protocol["common_conditions"]["observed_token_ceiling"] != 200_000:
        raise ValueError("Engine Selection token ceiling drift")
    smoke = protocol["mechanics_smoke"]
    if (
        smoke["attempt"] != "mechanics_smoke_v2"
        or smoke["token_call_launch_ceiling"] != 30_000
        or smoke["observed_token_hard_ceiling"] != 100_000
        or smoke["max_search_iterations"] != 3
        or smoke["replacement_for"]["outcome"] != "INVALID_MECHANICS_OR_ADAPTER"
    ):
        raise ValueError("Engine Selection smoke amendment drift")

    m4_protocols = _m4_protocols()
    expected_hashes = protocol["consumed_task_boundary"]["protocol_sha256"]
    actual_hashes = [sha256_file(path) for path in m4_protocols]
    if actual_hashes != expected_hashes:
        raise ValueError("M4 consumed-task boundary drift")
    consumed = consumed_m4_tasks()
    if smoke["task"] not in consumed or not bool(smoke["consumed_task_only"]):
        raise ValueError("mechanics smoke must use an already-consumed task")
    tasks = protocol["tasks"]
    if len(tasks) != 4:
        raise ValueError("Engine Selection requires three core tasks and one reserve")
    if sum(task["role"] == "core" for task in tasks) != 3:
        raise ValueError("Engine Selection requires exactly three core tasks")
    if sum(task["role"] == "reserve" for task in tasks) != 1:
        raise ValueError("Engine Selection requires exactly one reserve task")
    for task in tasks:
        name = str(task["task"])
        if name in consumed:
            raise ValueError(f"formal task was already consumed by M4: {name}")
        source = _source_path(name)
        if sha256_file(source) != task["source_sha256"]:
            raise ValueError(f"frozen task source drift: {name}")
        loaded = load_task(source, str(task["class"]))
        if type(loaded).__name__ != task["class"]:
            raise ValueError(f"task class drift: {name}")
    return protocol


def _task_names(protocol: dict[str, Any], role: str) -> list[str]:
    return [str(task["task"]) for task in protocol["tasks"] if task["role"] == role]


def _checkpoint(block: dict[str, Any], budget: int) -> dict[str, Any]:
    matches = [
        item for item in block["checkpoints"] if int(item["token_budget"]) == budget
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one {budget} checkpoint for "
            f"{block['task']}:{block['repeat']}:{block['arm']}"
        )
    return matches[0]


def _improvement(block: dict[str, Any], budget: int) -> float:
    checkpoint = _checkpoint(block, budget)
    if not bool(block["run_valid"]):
        return 0.0
    if int(block["observed_tokens"]) > budget and int(
        checkpoint["candidate_cumulative_tokens"]
    ) > budget:
        raise ValueError("checkpoint candidate exceeds its token budget")
    heldout = checkpoint["heldout"]
    if not bool(heldout["correct"]):
        return 0.0
    return max(0.0, float(heldout["raw_speedup"]) - 1.0)


def _anytime_auc(block: dict[str, Any]) -> float:
    points = [(0, 0.0)] + [
        (budget, _improvement(block, budget)) for budget in (50_000, 100_000, 200_000)
    ]
    area = sum(
        (right_x - left_x) * (left_y + right_y) / 2.0
        for (left_x, left_y), (right_x, right_y) in zip(points, points[1:])
    )
    return area / 200_000.0


def _index_blocks(blocks: list[dict[str, Any]]) -> dict[tuple[str, int, str], dict[str, Any]]:
    indexed: dict[tuple[str, int, str], dict[str, Any]] = {}
    for block in blocks:
        key = (str(block["task"]), int(block["repeat"]), str(block["arm"]))
        if key in indexed:
            raise ValueError(f"duplicate Engine Selection block: {key}")
        indexed[key] = block
    return indexed


def _task_scores(
    indexed: dict[tuple[str, int, str], dict[str, Any]],
    *,
    tasks: list[str],
    arms: list[str],
    repeats: tuple[int, ...],
    budget: int,
) -> dict[str, dict[str, float]]:
    return {
        task: {
            arm: fmean(_improvement(indexed[(task, repeat, arm)], budget) for repeat in repeats)
            for arm in arms
        }
        for task in tasks
    }


def _pairwise(
    task_scores: dict[str, dict[str, float]], arms: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    matrix: dict[str, dict[str, Any]] = {
        arm: {"wins": 0, "losses": 0, "ties": 0, "opponents": {}} for arm in arms
    }
    for left, right in combinations(arms, 2):
        left_wins = right_wins = ties = 0
        task_results: dict[str, str] = {}
        for task, scores in task_scores.items():
            if scores[left] > scores[right]:
                left_wins += 1
                task_results[task] = left
            elif scores[right] > scores[left]:
                right_wins += 1
                task_results[task] = right
            else:
                ties += 1
                task_results[task] = "TIE"
        matrix[left]["wins"] += left_wins
        matrix[left]["losses"] += right_wins
        matrix[left]["ties"] += ties
        matrix[right]["wins"] += right_wins
        matrix[right]["losses"] += left_wins
        matrix[right]["ties"] += ties
        matrix[left]["opponents"][right] = {
            "wins": left_wins,
            "losses": right_wins,
            "ties": ties,
            "tasks": task_results,
        }
        matrix[right]["opponents"][left] = {
            "wins": right_wins,
            "losses": left_wins,
            "ties": ties,
            "tasks": task_results,
        }
    copeland = {
        arm: int(row["wins"]) - int(row["losses"]) for arm, row in matrix.items()
    }
    return matrix, copeland


def _leaders(copeland: dict[str, int]) -> list[str]:
    best = max(copeland.values())
    return sorted(arm for arm, score in copeland.items() if score == best)


def _all_valid(
    indexed: dict[tuple[str, int, str], dict[str, Any]],
    tasks: list[str],
    repeats: tuple[int, ...],
    arm: str,
) -> bool:
    return all(bool(indexed[(task, repeat, arm)]["run_valid"]) for task in tasks for repeat in repeats)


def _ranked_arms(
    arms: list[str],
    copeland: dict[str, int],
    task_scores: dict[str, dict[str, float]],
) -> list[str]:
    # This ordering selects reserve participants only. Scientific wins remain
    # task-family pairwise results; aggregate magnitude is a deterministic tie break.
    return sorted(
        arms,
        key=lambda arm: (
            copeland[arm],
            fmean(scores[arm] for scores in task_scores.values()),
            arm == "vanilla",
            arm,
        ),
        reverse=True,
    )


def score_core(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    protocol = load_protocol()
    arms = [str(arm) for arm in protocol["arms"]]
    tasks = _task_names(protocol, "core")
    repeats = tuple(int(value) for value in protocol["core_repeats"])
    indexed = _index_blocks(blocks)
    expected = {(task, repeat, arm) for task in tasks for repeat in repeats for arm in arms}
    if set(indexed) != expected:
        missing = sorted(expected - set(indexed))
        extra = sorted(set(indexed) - expected)
        raise ValueError(f"core block coverage drift; missing={missing}, extra={extra}")

    budget_results: dict[str, Any] = {}
    for budget in (50_000, 100_000, 200_000):
        task_scores = _task_scores(
            indexed, tasks=tasks, arms=arms, repeats=repeats, budget=budget
        )
        matrix, copeland = _pairwise(task_scores, arms)
        budget_results[str(budget)] = {
            "task_scores": task_scores,
            "pairwise_matrix": matrix,
            "copeland": copeland,
            "leaders": _leaders(copeland),
        }

    deep = budget_results["200000"]
    cheap = budget_results["100000"]
    deep_leaders = deep["leaders"]
    reasons: list[str] = []
    if len(deep_leaders) != 1:
        reasons.append("HIGHEST_COPELAND_TIED")
    tentative = deep_leaders[0] if len(deep_leaders) == 1 else None
    if tentative and tentative != "vanilla":
        wins_vs_vanilla = deep["pairwise_matrix"][tentative]["opponents"]["vanilla"][
            "wins"
        ]
        if wins_vs_vanilla == 2:
            reasons.append("NON_VANILLA_REPLACEMENT_BOUNDARY_2_OF_3")
        if wins_vs_vanilla < 2:
            reasons.append("NON_VANILLA_DID_NOT_BEAT_VANILLA_ON_2_OF_3")
        if not _all_valid(indexed, tasks, repeats, tentative):
            reasons.append("TENTATIVE_LEADER_NOT_6_OF_6_VALID")
    if cheap["leaders"] != deep_leaders:
        reasons.append("100K_AND_200K_LEADERS_CONFLICT")

    ranked = _ranked_arms(arms, deep["copeland"], deep["task_scores"])
    reserve_reasons = [
        reason
        for reason in reasons
        if reason
        in {
            "HIGHEST_COPELAND_TIED",
            "NON_VANILLA_REPLACEMENT_BOUNDARY_2_OF_3",
            "100K_AND_200K_LEADERS_CONFLICT",
        }
    ]
    direct_global = "vanilla"
    if tentative and not reserve_reasons and _all_valid(indexed, tasks, repeats, tentative):
        if tentative == "vanilla":
            direct_global = "vanilla"
        else:
            wins = deep["pairwise_matrix"][tentative]["opponents"]["vanilla"]["wins"]
            if wins >= 2:
                direct_global = tentative

    def budget_default(result: dict[str, Any]) -> str:
        leaders = result["leaders"]
        if len(leaders) != 1:
            return "vanilla"
        leader = leaders[0]
        if not _all_valid(indexed, tasks, repeats, leader):
            return "vanilla"
        if leader != "vanilla":
            wins = result["pairwise_matrix"][leader]["opponents"]["vanilla"]["wins"]
            if wins < 2:
                return "vanilla"
        return leader

    arm_aggregates = {
        arm: {
            "core_runs_valid": sum(
                bool(indexed[(task, repeat, arm)]["run_valid"])
                for task in tasks
                for repeat in repeats
            ),
            "core_runs_required": 6,
            "observed_tokens_total": sum(
                int(indexed[(task, repeat, arm)]["observed_tokens"])
                for task in tasks
                for repeat in repeats
            ),
            "wall_seconds_total": sum(
                float(indexed[(task, repeat, arm)]["wall_seconds"])
                for task in tasks
                for repeat in repeats
            ),
            "anytime_auc_mean": fmean(
                _anytime_auc(indexed[(task, repeat, arm)])
                for task in tasks
                for repeat in repeats
            ),
        }
        for arm in arms
    }
    return {
        "schema_version": "1.0",
        "campaign": protocol["campaign"],
        "protocol_sha256": sha256_file(PROTOCOL),
        "stage": "CORE",
        "budget_results": budget_results,
        "arm_aggregates": arm_aggregates,
        "reserve_required": bool(reserve_reasons),
        "reserve_reasons": reserve_reasons,
        "reserve_participants": ranked[:2] if reserve_reasons else [],
        "CHEAP_DEFAULT_100K": budget_default(cheap),
        "DEEP_DEFAULT_200K": budget_default(deep),
        "GLOBAL_DEFAULT": "PENDING_RESERVE" if reserve_reasons else direct_global,
        "fallback_reasons": reasons,
        "claim_scope": protocol["claim_scope"],
        "superiority_claim_permitted": False,
        "mechanism_claim_permitted": False,
    }


def score_reserve(
    core_result: dict[str, Any], blocks: list[dict[str, Any]]
) -> dict[str, Any]:
    protocol = load_protocol()
    if core_result.get("protocol_sha256") != sha256_file(PROTOCOL):
        raise ValueError("reserve core-result protocol drift")
    if not bool(core_result.get("reserve_required")):
        raise ValueError("reserve cannot run without a frozen core trigger")
    participants = [str(arm) for arm in core_result["reserve_participants"]]
    if len(participants) != 2 or any(arm not in protocol["arms"] for arm in participants):
        raise ValueError("invalid reserve participants")
    task = _task_names(protocol, "reserve")[0]
    repeats = tuple(int(value) for value in protocol["reserve_repeats"])
    indexed = _index_blocks(blocks)
    expected = {(task, repeat, arm) for repeat in repeats for arm in participants}
    if set(indexed) != expected:
        raise ValueError("reserve block coverage drift")
    task_scores = _task_scores(
        indexed, tasks=[task], arms=participants, repeats=repeats, budget=200_000
    )
    matrix, copeland = _pairwise(task_scores, participants)
    leaders = _leaders(copeland)
    all_valid = {
        arm: _all_valid(indexed, [task], repeats, arm) for arm in participants
    }
    global_default = "vanilla"
    promotion_reason = "RESERVE_DID_NOT_ESTABLISH_REPLACEMENT"
    if "vanilla" not in participants:
        promotion_reason = "VANILLA_NOT_IN_RESERVE_PAIR_REPLACEMENT_NOT_EVALUABLE"
    elif len(leaders) == 1 and leaders[0] != "vanilla":
        challenger = leaders[0]
        core_wins = int(
            core_result["budget_results"]["200000"]["pairwise_matrix"][challenger][
                "opponents"
            ]["vanilla"]["wins"]
        )
        reserve_win = int(
            matrix[challenger]["opponents"]["vanilla"]["wins"] == 1
        )
        if core_wins + reserve_win >= 3 and all_valid[challenger] and all_valid["vanilla"]:
            global_default = challenger
            promotion_reason = "NON_VANILLA_WON_AT_LEAST_3_OF_4_VS_VANILLA"
    return {
        "schema_version": "1.0",
        "campaign": protocol["campaign"],
        "protocol_sha256": sha256_file(PROTOCOL),
        "stage": "RESERVE",
        "core_result_sha256": sha256_object(core_result),
        "participants": participants,
        "reserve_task": task,
        "task_scores_200k": task_scores,
        "pairwise_matrix_200k": matrix,
        "leaders_200k": leaders,
        "reserve_runs_valid": all_valid,
        "CHEAP_DEFAULT_100K": core_result["CHEAP_DEFAULT_100K"],
        "DEEP_DEFAULT_200K": core_result["DEEP_DEFAULT_200K"],
        "GLOBAL_DEFAULT": global_default,
        "promotion_reason": promotion_reason,
        "claim_scope": protocol["claim_scope"],
        "superiority_claim_permitted": False,
        "mechanism_claim_permitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ENGINE_SELECTION_R1 protocol and scorer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-protocol")
    score = subparsers.add_parser("score-core")
    score.add_argument("--blocks", type=Path, required=True)
    score.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "validate-protocol":
        protocol = load_protocol()
        result = {
            "campaign": protocol["campaign"],
            "protocol_sha256": sha256_file(PROTOCOL),
            "formal_tasks": [task["task"] for task in protocol["tasks"]],
            "status": "PASS",
        }
    else:
        result = score_core(_read_json(args.blocks))
        if args.output:
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
