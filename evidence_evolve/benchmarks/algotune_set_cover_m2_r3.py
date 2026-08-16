"""M2-R3 development-only basin quality conversion pilot for Set Cover."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.benchmarks import algotune_blind as benchmark
from evidence_evolve.discovery.async_autonomous import (
    AsyncAutonomousWaveRunner,
    AsyncWaveSlot,
    AsyncWaveSpec,
)
from evidence_evolve.discovery.campaign import CampaignCandidate, search_disposition
from evidence_evolve.discovery.m2_escape import M2ControllerTrace
from evidence_evolve.discovery.m2_r3_refine import (
    BasinAttempt,
    BasinRefinementStagedAdapter,
    BasinRetentionAudit,
    BasinRuntimeProfile,
    BasinState,
    M2R3AutonomousCampaignRunner,
    M2R3Policy,
    allocate_adaptive_slots,
    compare_profiles,
    profile_commit,
)
from evidence_evolve.discovery.throughput import FunnelStage, ThroughputPolicy
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import (
    ProtocolLock,
    dump_contract,
    load_contract,
)
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode
from evidence_evolve.models import Budgets, GateVerdict, MechanicsStatus, MutationType
from tasks.algotune_set_cover.common import DEVELOPMENT_SEEDS
from tasks.algotune_set_cover.staged_adapter import SetCoverFunnelPolicy


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = REPO_ROOT / "runs/algotune_set_cover_m2_r3_dev_v1"
R2B_ROOT = REPO_ROOT / "runs/algotune_set_cover_m2_r2b_pilot_dev_v1"
R2B_CAMPAIGN = R2B_ROOT / "campaign"
R2B_EXECUTION_REPO = R2B_ROOT / "execution_repo"
R1B_CAMPAIGN = (
    REPO_ROOT
    / "runs/algotune_set_cover_m2_r1b_dev_v1/controller_only/campaign"
)
POLICY_PATH = (
    REPO_ROOT
    / "research/policies/algotune_set_cover_m2_r3_basin_refine_v0.yaml"
)
GLOBAL_INCUMBENT_ID = "GEN-002-C01"
INITIAL_WAVE_IDS = ["R3-REFINE-01", "R3-REFINE-02", "R3-REFINE-03"]
ADAPTIVE_WAVE_ID = "R3-REFINE-ADAPTIVE"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _load_policy() -> M2R3Policy:
    return M2R3Policy.model_validate(
        yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    )


def _prepare_execution_repo(run_root: Path) -> Path:
    destination = run_root / "execution_repo"
    manifest_path = run_root / "execution_repo_context.json"
    payload = {
        "schema_version": "1.0",
        "source": str(R2B_EXECUTION_REPO.resolve()),
        "source_head": _git(R2B_EXECUTION_REPO, "rev-parse", "HEAD"),
        "r2b_contract_sha256": sha256_file(R2B_CAMPAIGN / "contract.locked.yaml"),
        "copy_preserves_r2b_and_r1b_candidate_commits": True,
    }
    if destination.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != payload:
            raise ValueError("M2-R3 execution context drift")
        return destination
    run_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(R2B_EXECUTION_REPO, destination, symlinks=True)
    _git(destination, "worktree", "prune")
    create_once_json(manifest_path, payload)
    return destination


def _runtime_contract(execution_repo: Path, run_root: Path, policy: M2R3Policy) -> Any:
    path = run_root / "campaign_contract.locked.yaml"
    if path.is_file():
        return load_contract(path)
    template = execution_repo / benchmark.CONTRACT_TEMPLATE.relative_to(REPO_ROOT)
    contract = load_contract(template)
    contract.campaign.id = "algotune-set-cover-m2-r3-basin-refine-16"
    contract.campaign.base_commit = _git(execution_repo, "rev-parse", "HEAD")
    contract.campaign.claim_scope = "SET_COVER_M2_R3_DEVELOPMENT_ONLY"
    contract.budgets = Budgets(
        proposal_calls=policy.total_proposal_slots,
        implementations=policy.total_proposal_slots,
        mechanics_runs=policy.total_proposal_slots,
        replications=1 + len(policy.basins) + policy.total_proposal_slots,
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_contract(locked, path)
    return locked


def _context_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proposal_path in sorted(run_dir.glob("generations/*/proposals/*.json")):
        candidate_id = proposal_path.stem
        receipts = sorted(
            (run_dir / "candidates" / candidate_id / "receipts").glob(
                "*.M0_MECHANICS.json"
            )
        )
        if not receipts:
            continue
        proposal = CampaignCandidate.model_validate_json(
            proposal_path.read_text(encoding="utf-8")
        )
        raw_receipt = json.loads(receipts[0].read_text(encoding="utf-8"))["receipt"]
        evaluation = raw_receipt["evaluation_input"]
        verdict = GateVerdict.model_validate(raw_receipt["verdict"])
        commit = raw_receipt.get("candidate_commit")
        patch_sha256 = raw_receipt.get("patch_sha256")
        score = evaluation.get("metrics", {}).get("raw_speedup")
        if commit is None or patch_sha256 is None or score is None:
            continue
        rows.append(
            {
                "candidate_id": candidate_id,
                "proposal": proposal,
                "receipt_path": receipts[0],
                "candidate_commit": str(commit),
                "patch_sha256": str(patch_sha256),
                "score": float(score),
                "protocol_valid": bool(verdict.protocol_valid),
                "data_eligible": bool(evaluation["data_eligible"]),
                "controls_pass": bool(evaluation["controls"])
                and all(bool(value) for value in evaluation["controls"].values()),
                "disposition": search_disposition(verdict),
                "scientific_outcome": verdict.scientific_outcome,
            }
        )
    return rows


def _import_context(
    runner: M2R3AutonomousCampaignRunner,
    policy: M2R3Policy,
) -> dict[str, dict[str, Any]]:
    rows = {
        row["candidate_id"]: row
        for run_dir in (R1B_CAMPAIGN, R2B_CAMPAIGN)
        for row in _context_rows(run_dir)
    }
    required_ids = {GLOBAL_INCUMBENT_ID} | {
        basin.root_candidate_id for basin in policy.basins
    }
    missing = sorted(required_ids - set(rows))
    if missing:
        raise ValueError(f"M2-R3 missing frozen basin roots: {missing}")
    for basin in policy.basins:
        row = rows[basin.root_candidate_id]
        genome = row["proposal"].acquisition.candidate
        if genome.family != basin.family:
            raise ValueError(f"M2-R3 basin family drift: {basin.basin_id}")
        if abs(row["score"] - basin.root_score) > 1e-12:
            raise ValueError(f"M2-R3 basin root score drift: {basin.basin_id}")
        if sha256_file(row["receipt_path"]) != basin.source_receipt_sha256:
            raise ValueError(f"M2-R3 basin source receipt drift: {basin.basin_id}")
    allowed_dispositions = set(policy.code_parent_dispositions)
    for row in sorted(rows.values(), key=lambda item: item["candidate_id"]):
        if not (
            row["protocol_valid"]
            and row["data_eligible"]
            and row["controls_pass"]
            and row["disposition"] in allowed_dispositions
        ):
            continue
        candidate = row["proposal"].acquisition.candidate
        _git(runner.repo_root, "cat-file", "-e", f"{row['candidate_commit']}^{{commit}}")
        prior_commit = runner._parent_commits.get(candidate.candidate_id)  # noqa: SLF001
        if prior_commit is not None:
            if prior_commit != row["candidate_commit"]:
                raise ValueError(f"M2-R3 imported parent commit drift: {candidate.candidate_id}")
            continue
        duplicate = runner.population.claim_code(
            candidate_id=candidate.candidate_id,
            generation_id=f"CONTEXT-{candidate.candidate_id}",
            code_sha256=row["patch_sha256"],
        )
        if duplicate is not None:
            raise ValueError(f"M2-R3 context code duplicate: {candidate.candidate_id}/{duplicate}")
        runner.population.admit(
            candidate=candidate,
            generation_id=f"CONTEXT-{candidate.candidate_id}",
            candidate_commit=row["candidate_commit"],
            code_sha256=row["patch_sha256"],
            search_disposition=row["disposition"],
            scientific_outcome=row["scientific_outcome"],
            acquisition_score=None,
            information_gain=row["proposal"].acquisition.signals.information_gain,
            novelty=row["proposal"].acquisition.signals.novelty,
            parent_dispositions=allowed_dispositions,
            stepping_stone_min_information_gain=policy.stepping_stone_min_information_gain,
            island_capacity=policy.island_capacity,
        )
        runner._parent_commits[candidate.candidate_id] = row[  # noqa: SLF001
            "candidate_commit"
        ]
    return rows


def _manifest(run_root: Path, policy: M2R3Policy) -> None:
    payload = {
        "schema_version": "1.0",
        "study_id": "algotune_set_cover_m2_r3_basin_quality_conversion_dev_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operator_class": "BASIN_REFINE",
        "operator_classes_excluded": ["STRUCTURAL_ESCAPE", "HYBRID_CROSSOVER"],
        "policy_sha256": sha256_file(POLICY_PATH),
        "policy": policy.model_dump(mode="json"),
        "primary_endpoint": {
            "basin_id": next(
                item.basin_id for item in policy.basins if item.primary_endpoint
            ),
            "conversion_threshold": policy.conversion_threshold,
            "threshold_rule": "PRIMARY_BASIN_DESCENDANT_RAW_SPEEDUP_GTE_THRESHOLD",
        },
        "model": benchmark.MODEL,
        "reasoning_effort": benchmark.REASONING_EFFORT,
        "evidence_scope": "DEVELOPMENT_ONLY",
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "blind_artifacts_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
    }
    path = run_root / "manifest.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        stable = {key: value for key, value in payload.items() if key != "created_at"}
        prior = {key: value for key, value in existing.items() if key != "created_at"}
        if prior != stable:
            raise ValueError("M2-R3 manifest drift")
    else:
        create_once_json(path, payload)


def _bind_controller_state(
    *,
    campaign_dir: Path,
    policy: M2R3Policy,
    wave_id: str,
    states: dict[str, BasinState],
    allocations: list[str],
) -> None:
    parents = [states[basin_id].local_incumbent_id for basin_id in allocations]
    unique_parents = list(dict.fromkeys(parents))
    trace = M2ControllerTrace(
        generation_id=wave_id,
        policy_id=policy.policy_id,
        mode=DiscoveryMode.NORMAL,
        incumbent_metric=policy.incumbent_metric,
        incumbent_value_before=policy.global_incumbent,
        stagnant_generations_before=0,
        escape_budget_remaining_before=0,
        escape_triggered=False,
        mutation_assignment=MutationType.MECHANISM,
        parent_pool=unique_parents,
        preferred_parent_ids=unique_parents,
        admitted_parent_ids=unique_parents,
        objective_values={
            states[basin_id].local_incumbent_id: states[
                basin_id
            ].local_incumbent_score
            for basin_id in allocations
        },
        root_lineages={
            states[basin_id].local_incumbent_id: states[basin_id].root_candidate_id
            for basin_id in allocations
        },
        required_structural_transition=False,
        required_seed_root=False,
    )
    path = campaign_dir / "generations" / wave_id / "m2_controller_state.json"
    if path.exists():
        if M2ControllerTrace.model_validate_json(path.read_text(encoding="utf-8")) != trace:
            raise ValueError("M2-R3 controller state drift")
    else:
        create_once_json(path, trace)


def _profile_summary(profile: BasinRuntimeProfile) -> dict[str, object]:
    return {
        "candidate_id": profile.candidate_id,
        "aggregate_speedup": profile.aggregate_speedup,
        "valid_instances": profile.valid_instances,
        "runtime_ns": {
            "p50": profile.candidate_time_p50_ns,
            "p90": profile.candidate_time_p90_ns,
            "p99": profile.candidate_time_p99_ns,
        },
        "per_instance": [
            {
                "seed": item.seed,
                "valid": item.valid,
                "candidate_time_ns": item.candidate_time_ns,
                "reference_time_ns": item.reference_time_ns,
                "speedup": item.speedup,
                "failure_type": item.failure_type,
            }
            for item in profile.instances
        ],
        "scientific_authority": "NONE_SCHEDULING_ONLY",
    }


def _feedback_packet(
    *,
    state: BasinState,
    profiles: dict[str, BasinRuntimeProfile],
    comparisons: dict[str, dict[str, object]],
) -> dict[str, object]:
    current = profiles[state.local_incumbent_id]
    last_attempt = state.attempts[-1] if state.attempts else None
    return {
        "basin_id": state.basin_id,
        "root_candidate_id": state.root_candidate_id,
        "local_incumbent_id": state.local_incumbent_id,
        "root_score": state.root_score,
        "local_incumbent_score": state.local_incumbent_score,
        "improvement_slope": state.improvement_slope,
        "consecutive_non_improving": state.consecutive_non_improving,
        "lineage_attempts": [item.model_dump(mode="json") for item in state.attempts],
        "local_incumbent_runtime_profile": _profile_summary(current),
        "versus_global_incumbent": comparisons.get(
            f"GLOBAL::{state.local_incumbent_id}"
        ),
        "last_parent_child_comparison": (
            comparisons.get(last_attempt.candidate_id) if last_attempt else None
        ),
        "feedback_authority": "NONE_SCHEDULING_ONLY",
    }


def _wave_spec(
    *,
    wave_id: str,
    allocations: list[str],
    states: dict[str, BasinState],
    profiles: dict[str, BasinRuntimeProfile],
    comparisons: dict[str, dict[str, object]],
    dispatch_offset: int,
) -> AsyncWaveSpec:
    slots: list[AsyncWaveSlot] = []
    for slot_index, basin_id in enumerate(allocations, start=1):
        state = states[basin_id]
        directive = json.dumps(
            {
                "basin_id": basin_id,
                "parent_candidate_id": state.local_incumbent_id,
                "lineage_feedback": _feedback_packet(
                    state=state,
                    profiles=profiles,
                    comparisons=comparisons,
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        slots.append(
            AsyncWaveSlot(
                slot=slot_index,
                dispatch_index=dispatch_offset + slot_index,
                operator_class="BASIN_REFINE",
                lineage_id=basin_id,
                island="main",
                eligible_parent_ids=[state.local_incumbent_id],
                primary_parent_id=state.local_incumbent_id,
                mutation=MutationType.MECHANISM,
                mode=DiscoveryMode.NORMAL,
                requires_structural_transition=False,
                operator_directive=directive,
            )
        )
    return AsyncWaveSpec(wave_id=wave_id, slots=slots)


def _throughput_policy(slot_count: int) -> ThroughputPolicy:
    return ThroughputPolicy(
        policy_id=f"m2-r3-basin-refine-{slot_count}-v0",
        total_candidate_budget=slot_count,
        propose_workers=min(4, slot_count),
        implement_workers=min(4, slot_count),
        l0_workers=min(4, slot_count),
        l1_workers=min(4, slot_count),
        l2_workers=min(4, slot_count),
        max_inflight_per_lineage=2,
        max_candidates_per_lineage=2,
        operator_quotas={"BASIN_REFINE": slot_count},
        shadow_audit_stride=None,
    )


def _ensure_profiles(
    *,
    runner: M2R3AutonomousCampaignRunner,
    policy: M2R3Policy,
    candidates: dict[str, str],
    profiles: dict[str, BasinRuntimeProfile],
) -> None:
    pending: list[tuple[str, str, Path]] = []
    profile_dir = runner.run_dir / "lineage_feedback"
    for candidate_id, commit in sorted(candidates.items()):
        runner.budgets.reserve(
            "replications", 1, f"replications:m2-r3-profile:{candidate_id}"
        )
        path = profile_dir / f"{candidate_id}.profile.json"
        if path.exists():
            profile = BasinRuntimeProfile.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if profile.candidate_commit != commit:
                raise ValueError(f"M2-R3 profile commit drift: {candidate_id}")
            profiles[candidate_id] = profile
        else:
            pending.append((candidate_id, commit, path))

    def run_one(item: tuple[str, str, Path]) -> tuple[str, Path, BasinRuntimeProfile]:
        candidate_id, commit, path = item
        profile = profile_commit(
            repo=runner.repo_root,
            candidate_id=candidate_id,
            candidate_commit=commit,
            seeds=list(DEVELOPMENT_SEEDS),
            repeats=policy.profile_repeats,
            workers=policy.profile_workers_per_candidate,
        )
        return candidate_id, path, profile

    if pending:
        with ThreadPoolExecutor(
            max_workers=min(policy.profile_parallel_candidates, len(pending)),
            thread_name_prefix="ee-r3-profile",
        ) as pool:
            for candidate_id, path, profile in pool.map(run_one, pending):
                create_once_json(path, profile)
                profiles[candidate_id] = profile


def _apply_wave_decisions(
    *,
    states: dict[str, BasinState],
    decisions: list[dict[str, Any]],
) -> None:
    for raw in decisions:
        basin_id = str(raw["basin_id"])
        attempt = BasinAttempt.model_validate(raw["attempt"])
        state = states[basin_id]
        state.attempts.append(attempt)
        if attempt.improved_local_incumbent:
            if attempt.score is None:
                raise ValueError("M2-R3 improvement is missing its score")
            state.local_incumbent_id = attempt.candidate_id
            state.local_incumbent_score = attempt.score
            state.consecutive_non_improving = 0
        else:
            state.consecutive_non_improving += 1


def _wave_decisions(
    *,
    wave: AsyncWaveSpec,
    wave_result: Any,
    states: dict[str, BasinState],
    policy: M2R3Policy,
    runner: M2R3AutonomousCampaignRunner,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    basin_by_candidate = {
        wave.candidate_id(slot): slot.lineage_id for slot in wave.slots
    }
    parent_by_candidate = {
        wave.candidate_id(slot): slot.primary_parent_id for slot in wave.slots
    }
    decisions: list[dict[str, Any]] = []
    profile_candidates: dict[str, str] = {}
    for record in wave_result.throughput.records:
        candidate_id = record.ticket.candidate_id
        basin_id = basin_by_candidate[candidate_id]
        l0 = record.decision(FunnelStage.L0)
        l2 = record.decision(FunnelStage.L2)
        basin_retained = bool(
            l0
            and l0.mechanics_status is MechanicsStatus.PASS
            and "BASIN_MECHANISM_ESCAPE_BLOCK" not in l0.reason_codes
        )
        dev_valid = bool(
            l2
            and l2.mechanics_status is MechanicsStatus.PASS
            and l2.data_eligible
            and l2.controls
            and all(l2.controls.values())
        )
        score = float(l2.metrics[policy.incumbent_metric]) if dev_valid and l2 else None
        improved = bool(
            basin_retained
            and score is not None
            and score
            >= states[basin_id].local_incumbent_score + policy.improvement_min_delta
        )
        attempt = BasinAttempt(
            candidate_id=candidate_id,
            parent_candidate_id=parent_by_candidate[candidate_id],
            wave_id=wave.wave_id,
            score=score,
            dev_valid=dev_valid,
            basin_retained=basin_retained,
            improved_local_incumbent=improved,
            terminal_stage=record.terminal_stage.value,
            terminal_status=record.terminal_status,
            failure_type=record.error_type,
            failure=record.error,
        )
        decisions.append(
            {"basin_id": basin_id, "attempt": attempt.model_dump(mode="json")}
        )
        if dev_valid:
            receipt_rel = wave_result.receipt_paths.get(candidate_id)
            if receipt_rel is None:
                raise ValueError(f"M2-R3 dev-valid candidate lacks receipt: {candidate_id}")
            receipt = json.loads(
                (runner.run_dir / receipt_rel).read_text(encoding="utf-8")
            )["receipt"]
            profile_candidates[candidate_id] = str(receipt["candidate_commit"])
    return decisions, profile_candidates


def run(run_root: Path, *, prepare_only: bool) -> dict[str, Any]:
    policy = _load_policy()
    _manifest(run_root, policy)
    result_path = run_root / "result.json"
    if result_path.is_file() and not prepare_only:
        return json.loads(result_path.read_text(encoding="utf-8"))
    execution_repo = _prepare_execution_repo(run_root)
    contract = _runtime_contract(execution_repo, run_root, policy)
    campaign_dir = run_root / "campaign"
    runner = M2R3AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(execution_repo / contract.closure_registry),
        policy=policy.frozen_base_policy(),
        r3_policy=policy,
        context_run_dirs=[R1B_CAMPAIGN, R2B_CAMPAIGN],
        repo_root=execution_repo,
        run_dir=campaign_dir,
        evaluate=benchmark.evaluate_ee,
        backend=benchmark._PinnedProotCodexBackend(),
        worktree_root=Path(tempfile.gettempdir()) / "ee-algotune-m2-r3-worktrees",
        reference_metrics={policy.incumbent_metric: policy.global_incumbent},
        memory_enabled=True,
        timeout_seconds=1_200,
    )
    rows = _import_context(runner, policy)
    states = {
        basin.basin_id: BasinState(
            basin_id=basin.basin_id,
            root_candidate_id=basin.root_candidate_id,
            local_incumbent_id=basin.root_candidate_id,
            root_score=basin.root_score,
            local_incumbent_score=basin.root_score,
        )
        for basin in policy.basins
    }
    preparation = {
        "operator_class": policy.operator_class,
        "slot_budget": policy.total_proposal_slots,
        "initial_wave_slots": policy.initial_waves * policy.slots_per_initial_wave,
        "adaptive_slots": policy.adaptive_slots,
        "primary_basin_id": next(
            item.basin_id for item in policy.basins if item.primary_endpoint
        ),
        "conversion_threshold": policy.conversion_threshold,
        "global_incumbent": policy.global_incumbent,
        "basin_roots": {
            item.basin_id: {
                "candidate_id": item.root_candidate_id,
                "score": item.root_score,
                "family": item.family,
            }
            for item in policy.basins
        },
        "throughput": {
            "cpu_process_quota": 18,
            "l2_workers": 4,
            "evaluator_workers_per_l2": policy.profile_workers_per_candidate,
            "maximum_nested_evaluator_workers": 16,
            "proposal_and_evaluation_queues_separate": True,
        },
        "evidence_scope": "DEVELOPMENT_ONLY",
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    preparation_path = run_root / "preparation.json"
    if preparation_path.exists():
        if json.loads(preparation_path.read_text(encoding="utf-8")) != preparation:
            raise ValueError("M2-R3 preparation drift")
    else:
        create_once_json(preparation_path, preparation)
    if prepare_only:
        return preparation

    started = time.perf_counter()
    profiles: dict[str, BasinRuntimeProfile] = {}
    comparisons: dict[str, dict[str, object]] = {}
    initial_profile_candidates = {
        GLOBAL_INCUMBENT_ID: rows[GLOBAL_INCUMBENT_ID]["candidate_commit"],
        **{
            basin.root_candidate_id: rows[basin.root_candidate_id][
                "candidate_commit"
            ]
            for basin in policy.basins
        },
    }
    _ensure_profiles(
        runner=runner,
        policy=policy,
        candidates=initial_profile_candidates,
        profiles=profiles,
    )
    for basin in policy.basins:
        comparisons[f"GLOBAL::{basin.root_candidate_id}"] = compare_profiles(
            profiles[GLOBAL_INCUMBENT_ID], profiles[basin.root_candidate_id]
        )

    wave_summaries: list[dict[str, object]] = []
    scheduled_slots = 0
    waves: list[tuple[str, list[str]]] = [
        (wave_id, [item.basin_id for item in policy.basins])
        for wave_id in INITIAL_WAVE_IDS
    ]
    for wave_index in range(len(INITIAL_WAVE_IDS) + 1):
        if wave_index == len(INITIAL_WAVE_IDS):
            allocation_path = run_root / "adaptive_allocation.json"
            if allocation_path.exists():
                allocations = [
                    str(item)
                    for item in json.loads(
                        allocation_path.read_text(encoding="utf-8")
                    )["basin_ids"]
                ]
            else:
                allocations = allocate_adaptive_slots(states, policy)
                create_once_json(
                    allocation_path,
                    {
                        "basin_ids": allocations,
                        "slope_ranking": [
                            {
                                "basin_id": state.basin_id,
                                "improvement_slope": state.improvement_slope,
                                "consecutive_non_improving": state.consecutive_non_improving,
                            }
                            for state in sorted(
                                states.values(),
                                key=lambda item: (
                                    -item.improvement_slope,
                                    -item.local_incumbent_score,
                                    item.basin_id,
                                ),
                            )
                        ],
                        "unallocated_slots": policy.adaptive_slots - len(allocations),
                        "no_replacement_slots": True,
                    },
                )
            wave_id = ADAPTIVE_WAVE_ID
        else:
            wave_id, allocations = waves[wave_index]
        if not allocations:
            continue
        _bind_controller_state(
            campaign_dir=campaign_dir,
            policy=policy,
            wave_id=wave_id,
            states=states,
            allocations=allocations,
        )
        wave = _wave_spec(
            wave_id=wave_id,
            allocations=allocations,
            states=states,
            profiles=profiles,
            comparisons=comparisons,
            dispatch_offset=scheduled_slots,
        )
        decision_path = run_root / "wave_decisions" / f"{wave_id}.json"
        if decision_path.exists():
            decision_payload = json.loads(decision_path.read_text(encoding="utf-8"))
            decisions = list(decision_payload["decisions"])
            profile_candidates = {
                str(key): str(value)
                for key, value in decision_payload["profile_candidates"].items()
            }
            wave_summary = dict(decision_payload["wave_summary"])
        else:
            local_scores = {
                basin_id: states[basin_id].local_incumbent_score
                for basin_id in set(allocations)
            }
            adapter = BasinRefinementStagedAdapter(
                contract=contract,
                policy=SetCoverFunnelPolicy(
                    incumbent_speedup=policy.global_incumbent,
                    probe_min_speedup=0.0,
                ),
                local_incumbents=local_scores,
                probe_parent_fraction=policy.probe_parent_fraction,
                retention_audit=BasinRetentionAudit(policy),
            )
            throughput_policy = _throughput_policy(len(allocations))
            wave_result = AsyncAutonomousWaveRunner(
                runner=runner,
                throughput_policy=throughput_policy,
                staged_adapter=adapter,
            ).run_wave(
                wave=wave,
                feedback={
                    "operator_class": "BASIN_REFINE",
                    "global_incumbent": policy.global_incumbent,
                    "conversion_threshold": policy.conversion_threshold,
                    "evidence_scope": "DEVELOPMENT_ONLY",
                    "scientific_authority": "NONE_SCHEDULING_ONLY",
                },
            )
            decisions, profile_candidates = _wave_decisions(
                wave=wave,
                wave_result=wave_result,
                states=states,
                policy=policy,
                runner=runner,
            )
            wave_summary = {
                "wave_id": wave_id,
                "allocations": allocations,
                "metrics": wave_result.throughput.metrics.model_dump(mode="json"),
                "resources": wave_result.throughput.resources,
            }
            create_once_json(
                decision_path,
                {
                    "decisions": decisions,
                    "profile_candidates": profile_candidates,
                    "wave_summary": wave_summary,
                },
            )
        _ensure_profiles(
            runner=runner,
            policy=policy,
            candidates=profile_candidates,
            profiles=profiles,
        )
        for raw in decisions:
            attempt = BasinAttempt.model_validate(raw["attempt"])
            if attempt.candidate_id not in profiles:
                continue
            parent_profile = profiles[attempt.parent_candidate_id]
            child_profile = profiles[attempt.candidate_id]
            comparison = compare_profiles(parent_profile, child_profile)
            comparisons[attempt.candidate_id] = comparison
            comparison_path = (
                campaign_dir
                / "lineage_feedback"
                / f"{attempt.candidate_id}.parent_delta.json"
            )
            if comparison_path.exists():
                if json.loads(comparison_path.read_text(encoding="utf-8")) != comparison:
                    raise ValueError("M2-R3 parent comparison drift")
            else:
                create_once_json(comparison_path, comparison)
        _apply_wave_decisions(states=states, decisions=decisions)
        for state in states.values():
            if state.local_incumbent_id in profiles:
                comparisons[f"GLOBAL::{state.local_incumbent_id}"] = compare_profiles(
                    profiles[GLOBAL_INCUMBENT_ID], profiles[state.local_incumbent_id]
                )
        scheduled_slots += len(allocations)
        wave_summaries.append(wave_summary)

    primary = next(
        states[item.basin_id] for item in policy.basins if item.primary_endpoint
    )
    supported = primary.local_incumbent_score >= policy.conversion_threshold
    payload = {
        "schema_version": "1.0",
        "interpretation_status": (
            "BASIN_QUALITY_CONVERSION_SUPPORTED"
            if supported
            else "BASIN_QUALITY_CONVERSION_NOT_SUPPORTED"
        ),
        "interpretation_is_not_scientific_outcome": True,
        "primary_endpoint": {
            "basin_id": primary.basin_id,
            "root_score": primary.root_score,
            "best_descendant_score": primary.local_incumbent_score,
            "conversion_threshold": policy.conversion_threshold,
            "threshold_met": supported,
            "strict_improvement_count": sum(
                item.improved_local_incumbent for item in primary.attempts
            ),
        },
        "basins": {
            basin_id: {
                **state.model_dump(mode="json"),
                "improvement_slope": state.improvement_slope,
            }
            for basin_id, state in sorted(states.items())
        },
        "scheduled_slots": scheduled_slots,
        "unspent_slots": policy.total_proposal_slots - scheduled_slots,
        "no_retries_or_replacement_samples": True,
        "waves": wave_summaries,
        "budgets": runner.budgets.snapshot(),
        "tokens": benchmark._token_usage(campaign_dir),
        "wall_seconds": time.perf_counter() - started,
        "evidence_scope": "DEVELOPMENT_ONLY",
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "blind_artifacts_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
    }
    create_once_json(result_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the development-only M2-R3 basin refinement pilot"
    )
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    for key, value in {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "EE_ALGOTUNE_DEV_COUNT": "100",
        "EE_ALGOTUNE_DEV_REPEATS": "3",
        "EE_ALGOTUNE_WORKERS": "4",
    }.items():
        os.environ[key] = value
    payload = run(args.run_dir.resolve(), prepare_only=args.prepare_only)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
