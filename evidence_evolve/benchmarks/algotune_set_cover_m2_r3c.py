"""M2-R3C activation-gated basin quality conversion repair for Set Cover."""

from __future__ import annotations

import argparse
import hashlib
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
from evidence_evolve.benchmarks.algotune_set_cover_m2_r3 import (
    GLOBAL_INCUMBENT_ID,
    R1B_CAMPAIGN,
    R2B_CAMPAIGN,
    R2B_EXECUTION_REPO,
    _import_context,
)
from evidence_evolve.discovery.async_autonomous import (
    AsyncAutonomousWaveRunner,
    AsyncWaveSlot,
    AsyncWaveSpec,
)
from evidence_evolve.discovery.m2_escape import M2ControllerTrace
from evidence_evolve.discovery.m2_r3_refine import (
    BasinRetentionAudit,
    BasinRuntimeProfile,
    compare_profiles,
    profile_commit,
)
from evidence_evolve.discovery.m2_r3c_refine import (
    AttemptFeedback,
    BasinActivationProfile,
    BasinRefinementBrief,
    BasinRefinementCStagedAdapter,
    M2R3CAutonomousCampaignRunner,
    M2R3CPolicy,
    StageFeedback,
    activation_profile_commit,
    run_exactness_canary,
)
from evidence_evolve.discovery.throughput import (
    CandidateFunnelRecord,
    FunnelStage,
    ThroughputPolicy,
)
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import (
    ProtocolLock,
    dump_contract,
    load_contract,
)
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode
from evidence_evolve.models import Budgets, MechanicsStatus, MutationType
from evidence_evolve.proposals.admission import available_cpu_count
from tasks.algotune_set_cover.common import DEVELOPMENT_SEEDS
from tasks.algotune_set_cover.staged_adapter import SetCoverFunnelPolicy


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = REPO_ROOT / "runs/algotune_set_cover_m2_r3c_dev_v1"
POLICY_PATH = (
    REPO_ROOT
    / "research/policies/algotune_set_cover_m2_r3c_basin_refine_v0.yaml"
)
WAVE_IDS = [f"R3C-REFINE-{index:02d}" for index in range(1, 5)]


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _load_policy() -> M2R3CPolicy:
    return M2R3CPolicy.model_validate(
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
        "copy_preserves_frozen_context_commits": True,
    }
    if destination.exists():
        if not manifest_path.is_file() or json.loads(
            manifest_path.read_text(encoding="utf-8")
        ) != payload:
            raise ValueError("M2-R3C execution context drift")
        return destination
    run_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(R2B_EXECUTION_REPO, destination, symlinks=True)
    _git(destination, "worktree", "prune")
    create_once_json(manifest_path, payload)
    return destination


def _runtime_contract(execution_repo: Path, run_root: Path, policy: M2R3CPolicy) -> Any:
    path = run_root / "campaign_contract.locked.yaml"
    if path.is_file():
        return load_contract(path)
    template = execution_repo / benchmark.CONTRACT_TEMPLATE.relative_to(REPO_ROOT)
    contract = load_contract(template)
    contract.campaign.id = "algotune-set-cover-m2-r3c-active-basin-refine-4"
    contract.campaign.base_commit = _git(execution_repo, "rev-parse", "HEAD")
    contract.campaign.claim_scope = "SET_COVER_M2_R3C_DEVELOPMENT_ONLY"
    contract.budgets = Budgets(
        proposal_calls=policy.candidate_slots * 2,
        implementations=policy.candidate_slots,
        mechanics_runs=policy.candidate_slots,
        replications=3 + policy.candidate_slots * 2,
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_contract(locked, path)
    return locked


def _manifest(run_root: Path, policy: M2R3CPolicy) -> None:
    payload = {
        "schema_version": "1.0",
        "study_id": "algotune_set_cover_m2_r3c_active_basin_conversion_dev_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operator_class": policy.operator_class,
        "policy_sha256": sha256_file(POLICY_PATH),
        "policy": policy.model_dump(mode="json"),
        "repair_scope": [
            "ACTIVE_MECHANISM_ADMISSION",
            "DECODED_BOUNDED_FEEDBACK",
            "FULL_STAGE_FEEDBACK",
            "INDEPENDENT_EXACTNESS_CANARY",
        ],
        "excluded_controls": [
            "meet_in_the_middle_inactive_on_development_distribution",
            "component_decomposition_inactive_on_development_distribution",
            "incidence_signature_inactive_on_development_distribution",
        ],
        "evidence_scope": "DEVELOPMENT_ONLY",
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "blind_artifacts_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
    }
    path = run_root / "manifest.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        for item in (payload, existing):
            item.pop("created_at", None)
        if existing != payload:
            raise ValueError("M2-R3C manifest drift")
    else:
        create_once_json(path, payload)


def _activation_pass(profile: BasinActivationProfile, policy: M2R3CPolicy) -> bool:
    return bool(
        profile.activation_rate >= policy.minimum_activation_rate
        and profile.fallback_rate <= policy.maximum_fallback_rate
        and profile.invalid_rate == 0.0
    )


def _bind_controller(
    *,
    campaign_dir: Path,
    policy: M2R3CPolicy,
    wave_id: str,
    parent_id: str,
    parent_score: float,
) -> None:
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
        parent_pool=[parent_id],
        preferred_parent_ids=[parent_id],
        admitted_parent_ids=[parent_id],
        objective_values={parent_id: parent_score},
        root_lineages={parent_id: policy.basins[0].root_candidate_id},
        required_structural_transition=False,
        required_seed_root=False,
    )
    path = campaign_dir / "generations" / wave_id / "m2_controller_state.json"
    if path.exists():
        if M2ControllerTrace.model_validate_json(
            path.read_text(encoding="utf-8")
        ) != trace:
            raise ValueError("M2-R3C controller state drift")
    else:
        create_once_json(path, trace)


def _throughput_policy() -> ThroughputPolicy:
    return ThroughputPolicy(
        policy_id="m2-r3c-active-basin-refine-1-v0",
        total_candidate_budget=1,
        propose_workers=1,
        implement_workers=1,
        l0_workers=1,
        l1_workers=1,
        l2_workers=1,
        max_inflight_per_lineage=1,
        max_candidates_per_lineage=1,
        operator_quotas={"BASIN_REFINE_TELEMETRY": 1},
        shadow_audit_stride=None,
    )


def _stage_feedback(record: CandidateFunnelRecord) -> list[StageFeedback]:
    decisions = {item.stage: item for item in record.decisions}
    stages: list[StageFeedback] = []
    order = list(FunnelStage)
    terminal_index = order.index(record.terminal_stage)
    for index, stage in enumerate(order):
        if index > terminal_index:
            break
        decision = decisions.get(stage)
        if decision is not None:
            stages.append(
                StageFeedback(
                    stage=stage.value,
                    status=decision.status.value,
                    mechanics_status=decision.mechanics_status,
                    data_eligible=decision.data_eligible,
                    controls=decision.controls,
                    metrics=decision.metrics,
                    scientific_outcome=decision.scientific_outcome,
                    reason_codes=decision.reason_codes,
                )
            )
            continue
        failed = stage is record.terminal_stage and record.terminal_status == "FAILED"
        stages.append(
            StageFeedback(
                stage=stage.value,
                status="FAILED" if failed else "PASS",
                data_eligible=False,
                controls={},
                metrics={},
                reason_codes=[record.error_type] if failed and record.error_type else [],
            )
        )
    return stages


def _candidate_commit(repo: Path, campaign_dir: Path, candidate_id: str) -> str | None:
    namespace = hashlib.sha256(
        str(campaign_dir.resolve()).encode("utf-8")
    ).hexdigest()[:8]
    reference = f"refs/evidence-evolve/{namespace}/{candidate_id}"
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", reference],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _save_profile(
    *,
    runner: M2R3CAutonomousCampaignRunner,
    policy: M2R3CPolicy,
    candidate_id: str,
    commit: str,
    workers: int,
) -> BasinRuntimeProfile:
    path = runner.run_dir / "lineage_feedback" / f"{candidate_id}.profile.json"
    runner.budgets.reserve(
        "replications", 1, f"replications:r3c-runtime:{candidate_id}"
    )
    if path.exists():
        profile = BasinRuntimeProfile.model_validate_json(path.read_text(encoding="utf-8"))
        if profile.candidate_commit != commit:
            raise ValueError(f"R3C runtime profile commit drift: {candidate_id}")
        return profile
    profile = profile_commit(
        repo=runner.repo_root,
        candidate_id=candidate_id,
        candidate_commit=commit,
        seeds=list(DEVELOPMENT_SEEDS),
        repeats=3,
        workers=workers,
    )
    create_once_json(path, profile)
    return profile


def _save_activation(
    *,
    runner: M2R3CAutonomousCampaignRunner,
    policy: M2R3CPolicy,
    candidate_id: str,
    commit: str,
    workers: int,
) -> BasinActivationProfile:
    path = runner.run_dir / "activation_profiles" / f"{candidate_id}.json"
    runner.budgets.reserve(
        "replications", 1, f"replications:r3c-activation:{candidate_id}"
    )
    if path.exists():
        profile = BasinActivationProfile.model_validate_json(path.read_text(encoding="utf-8"))
        if profile.candidate_commit != commit:
            raise ValueError(f"R3C activation profile commit drift: {candidate_id}")
        return profile
    profile = activation_profile_commit(
        repo=runner.repo_root,
        candidate_id=candidate_id,
        candidate_commit=commit,
        seeds=list(DEVELOPMENT_SEEDS),
        workers=workers,
    )
    create_once_json(path, profile)
    return profile


def _profile_pair(
    *,
    runner: M2R3CAutonomousCampaignRunner,
    policy: M2R3CPolicy,
    candidate_id: str,
    commit: str,
) -> tuple[BasinRuntimeProfile, BasinActivationProfile]:
    workers = min(policy.telemetry_workers, max(1, available_cpu_count() // 2))
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ee-r3c-telemetry") as pool:
        runtime_future = pool.submit(
            _save_profile,
            runner=runner,
            policy=policy,
            candidate_id=candidate_id,
            commit=commit,
            workers=workers,
        )
        activation_future = pool.submit(
            _save_activation,
            runner=runner,
            policy=policy,
            candidate_id=candidate_id,
            commit=commit,
            workers=workers,
        )
        return runtime_future.result(), activation_future.result()


def _brief(
    *,
    policy: M2R3CPolicy,
    candidate_id: str,
    parent_id: str,
    parent_score: float,
    parent_profile: BasinRuntimeProfile,
    parent_activation: BasinActivationProfile,
    versus_global: dict[str, object],
    last_attempt: AttemptFeedback | None,
    last_delta: dict[str, object] | None,
) -> BasinRefinementBrief:
    largest_losses = list(versus_global.get("largest_losses", []))
    largest_wins = list(versus_global.get("largest_wins", []))
    regression_ids = [int(item["seed"]) for item in largest_losses[:10]]
    if not regression_ids:
        regression_ids = [int(item.seed) for item in parent_profile.instances[:10]]
    improvement_ids = [int(item["seed"]) for item in largest_wins[:10]]
    counters = {
        "parent_raw_speedup": parent_score,
        "gap_to_global_raw_speedup": policy.global_incumbent - parent_score,
        "activation_rate": parent_activation.activation_rate,
        "primary_completion_rate": parent_activation.primary_completion_rate,
        "fallback_rate": parent_activation.fallback_rate,
        "invalid_rate": parent_activation.invalid_rate,
        "mean_kernel_search_calls": parent_activation.mean_kernel_search_calls,
        "mean_kernel_search_successes": parent_activation.mean_kernel_search_successes,
        "mean_propagation_calls": parent_activation.mean_propagation_calls,
        "mean_forced_events": parent_activation.mean_forced_events,
        "runtime_p50_ns": float(parent_profile.candidate_time_p50_ns),
        "runtime_p90_ns": float(parent_profile.candidate_time_p90_ns),
        "runtime_p99_ns": float(parent_profile.candidate_time_p99_ns),
        "global_loss_count": float(versus_global.get("loss_count", 0)),
        "global_win_count": float(versus_global.get("win_count", 0)),
    }
    compact_delta = None
    if last_delta is not None:
        compact_delta = {
            key: last_delta[key]
            for key in (
                "parent_candidate_id",
                "child_candidate_id",
                "win_count",
                "loss_count",
                "tie_count",
                "invalid_or_unshared_count",
                "parent_runtime_ns",
                "child_runtime_ns",
                "largest_wins",
                "largest_losses",
            )
            if key in last_delta
        }
    seed = BasinRefinementBrief(
        brief_id="0" * 16,
        candidate_id=candidate_id,
        basin_id=policy.basins[0].basin_id,
        parent_candidate_id=parent_id,
        parent_score=parent_score,
        available_counters=counters,
        regression_seed_ids=regression_ids,
        improvement_seed_ids=improvement_ids,
        parent_runtime_ns={
            "p50": parent_profile.candidate_time_p50_ns,
            "p90": parent_profile.candidate_time_p90_ns,
            "p99": parent_profile.candidate_time_p99_ns,
        },
        activation_gate={
            "minimum_activation_rate": policy.minimum_activation_rate,
            "maximum_fallback_rate": policy.maximum_fallback_rate,
            "requires_zero_invalid_rate": True,
            "parent_passed": _activation_pass(parent_activation, policy),
        },
        last_attempt=last_attempt,
        last_parent_child_delta=compact_delta,
        required_plan_fields=[
            "observed_counter",
            "target_counter",
            "addressed_seed_ids",
            "mechanism_change",
            "correctness_invariants",
            "falsifier",
        ],
    )
    brief_id = sha256_object(seed.model_dump(mode="json", exclude={"brief_id"}))[:16]
    result = seed.model_copy(update={"brief_id": brief_id})
    result.assert_size(policy.feedback_brief_max_bytes)
    return result


def _wave(
    *,
    wave_id: str,
    dispatch_index: int,
    basin_id: str,
    parent_id: str,
    brief_path: Path,
) -> AsyncWaveSpec:
    directive = json.dumps(
        {
            "basin_id": basin_id,
            "parent_candidate_id": parent_id,
            "brief_sha256": sha256_file(brief_path),
        },
        sort_keys=True,
    )
    return AsyncWaveSpec(
        wave_id=wave_id,
        slots=[
            AsyncWaveSlot(
                slot=1,
                dispatch_index=dispatch_index,
                operator_class="BASIN_REFINE_TELEMETRY",
                lineage_id=basin_id,
                island="main",
                eligible_parent_ids=[parent_id],
                primary_parent_id=parent_id,
                mutation=MutationType.MECHANISM,
                mode=DiscoveryMode.NORMAL,
                requires_structural_transition=False,
                operator_directive=directive,
            )
        ],
    )


def _root_exactness(
    *, repo: Path, commit: str, path: Path
) -> dict[str, object]:
    source = subprocess.run(
        ["git", "show", f"{commit}:tasks/algotune_set_cover/initial.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="ee-r3c-root-canary-") as temp_dir:
        candidate_path = Path(temp_dir) / "candidate.py"
        candidate_path.write_bytes(source)
        result = run_exactness_canary(candidate_path)
    if path.exists():
        frozen = json.loads(path.read_text(encoding="utf-8"))
        if frozen != result.model_dump(mode="json"):
            raise ValueError("R3C root exactness canary drift")
    else:
        create_once_json(path, result)
    return result.model_dump(mode="json")


def run(run_root: Path, *, prepare_only: bool) -> dict[str, Any]:
    policy = _load_policy()
    _manifest(run_root, policy)
    result_path = run_root / "result.json"
    if result_path.is_file() and not prepare_only:
        return json.loads(result_path.read_text(encoding="utf-8"))
    execution_repo = _prepare_execution_repo(run_root)
    contract = _runtime_contract(execution_repo, run_root, policy)
    campaign_dir = run_root / "campaign"
    runner = M2R3CAutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(execution_repo / contract.closure_registry),
        policy=policy.frozen_base_policy(),
        r3c_policy=policy,
        context_run_dirs=[R1B_CAMPAIGN, R2B_CAMPAIGN],
        repo_root=execution_repo,
        run_dir=campaign_dir,
        evaluate=benchmark.evaluate_ee,
        backend=benchmark._PinnedProotCodexBackend(),
        worktree_root=Path(tempfile.gettempdir()) / "ee-algotune-m2-r3c-worktrees",
        reference_metrics={policy.incumbent_metric: policy.global_incumbent},
        memory_enabled=True,
        timeout_seconds=1_200,
    )
    rows = _import_context(runner, policy)  # type: ignore[arg-type]
    basin = policy.basins[0]
    preparation = {
        "operator_class": policy.operator_class,
        "candidate_slots": policy.candidate_slots,
        "sequential_descendants": True,
        "early_stop_non_improving_attempts": policy.early_stop_non_improving_attempts,
        "active_basin": basin.model_dump(mode="json"),
        "conversion_threshold": policy.conversion_threshold,
        "activation_gate": {
            "minimum_activation_rate": policy.minimum_activation_rate,
            "maximum_fallback_rate": policy.maximum_fallback_rate,
            "requires_zero_invalid_rate": True,
        },
        "exactness_canary": {
            "exhaustive_universe_size": policy.exactness_exhaustive_universe_size,
            "exhaustive_max_family_size": policy.exactness_exhaustive_max_family_size,
            "known_r3_pivot_exactness_regression": True,
        },
        "feedback_transport": "DIRECT_DECODED_BOUNDED_BRIEF",
        "feedback_brief_max_bytes": policy.feedback_brief_max_bytes,
        "usable_cpu_count": available_cpu_count(),
        "proposal_and_evaluation_queues_separate": True,
        "evidence_scope": "DEVELOPMENT_ONLY",
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    preparation_path = run_root / "preparation.json"
    if preparation_path.exists():
        if json.loads(preparation_path.read_text(encoding="utf-8")) != preparation:
            raise ValueError("M2-R3C preparation drift")
    else:
        create_once_json(preparation_path, preparation)
    if prepare_only:
        return preparation

    started = time.perf_counter()
    root_id = basin.root_candidate_id
    root_commit = str(rows[root_id]["candidate_commit"])
    global_commit = str(rows[GLOBAL_INCUMBENT_ID]["candidate_commit"])
    root_canary = _root_exactness(
        repo=runner.repo_root,
        commit=root_commit,
        path=campaign_dir / "exactness_canaries" / f"{root_id}.json",
    )
    root_profile, root_activation = _profile_pair(
        runner=runner,
        policy=policy,
        candidate_id=root_id,
        commit=root_commit,
    )
    global_workers = min(policy.telemetry_workers, available_cpu_count())
    global_profile = _save_profile(
        runner=runner,
        policy=policy,
        candidate_id=GLOBAL_INCUMBENT_ID,
        commit=global_commit,
        workers=global_workers,
    )
    if not root_canary["passed"] or not _activation_pass(root_activation, policy):
        payload = {
            "schema_version": "1.0",
            "interpretation_status": "INVALID_MECHANICS_OR_ADAPTER",
            "reason": "ACTIVE_BASIN_ROOT_FAILED_R3C_ADMISSION",
            "root_exactness": root_canary,
            "root_activation": root_activation.model_dump(mode="json"),
            "scheduled_slots": 0,
            "budgets": runner.budgets.snapshot(),
            "evidence_scope": "DEVELOPMENT_ONLY",
            "scientific_authority": "NONE_SCHEDULING_ONLY",
            "blind_artifacts_read": False,
            "confirmation_runs": 0,
        }
        create_once_json(result_path, payload)
        return payload

    profiles = {root_id: root_profile, GLOBAL_INCUMBENT_ID: global_profile}
    activations = {root_id: root_activation}
    parent_id = root_id
    parent_score = basin.root_score
    consecutive_non_improving = 0
    attempts: list[AttemptFeedback] = []
    last_delta: dict[str, object] | None = None
    wave_summaries: list[dict[str, object]] = []

    for dispatch_index, wave_id in enumerate(WAVE_IDS, start=1):
        if consecutive_non_improving >= policy.early_stop_non_improving_attempts:
            break
        candidate_id = f"{wave_id}-C01"
        versus_global = compare_profiles(global_profile, profiles[parent_id])
        brief = _brief(
            policy=policy,
            candidate_id=candidate_id,
            parent_id=parent_id,
            parent_score=parent_score,
            parent_profile=profiles[parent_id],
            parent_activation=activations[parent_id],
            versus_global=versus_global,
            last_attempt=attempts[-1] if attempts else None,
            last_delta=last_delta,
        )
        brief_path = (
            campaign_dir
            / "generations"
            / wave_id
            / "r3c_feedback"
            / f"{candidate_id}.brief.json"
        )
        if brief_path.exists():
            if BasinRefinementBrief.model_validate_json(
                brief_path.read_text(encoding="utf-8")
            ) != brief:
                raise ValueError(f"R3C brief drift: {candidate_id}")
        else:
            create_once_json(brief_path, brief)
        _bind_controller(
            campaign_dir=campaign_dir,
            policy=policy,
            wave_id=wave_id,
            parent_id=parent_id,
            parent_score=parent_score,
        )
        wave = _wave(
            wave_id=wave_id,
            dispatch_index=dispatch_index,
            basin_id=basin.basin_id,
            parent_id=parent_id,
            brief_path=brief_path,
        )
        decision_path = run_root / "wave_decisions" / f"{wave_id}.json"
        if decision_path.exists():
            saved = json.loads(decision_path.read_text(encoding="utf-8"))
            attempt = AttemptFeedback.model_validate(saved["attempt"])
            candidate_score = saved.get("candidate_score")
            activation_admitted = bool(saved.get("activation_admitted", False))
            improved = attempt.local_incumbent_improved
            candidate_delta = saved.get("parent_child_delta")
            wave_summary = dict(saved["wave_summary"])
            if attempt.candidate_commit is not None:
                candidate_profile, candidate_activation = _profile_pair(
                    runner=runner,
                    policy=policy,
                    candidate_id=candidate_id,
                    commit=attempt.candidate_commit,
                )
                profiles[candidate_id] = candidate_profile
                activations[candidate_id] = candidate_activation
        else:
            adapter = BasinRefinementCStagedAdapter(
                contract=contract,
                policy=SetCoverFunnelPolicy(
                    incumbent_speedup=policy.global_incumbent,
                    probe_min_speedup=0.0,
                ),
                local_incumbents={basin.basin_id: parent_score},
                probe_parent_fraction=policy.probe_parent_fraction,
                retention_audit=BasinRetentionAudit(policy),  # type: ignore[arg-type]
                canary_dir=campaign_dir / "exactness_canaries",
            )
            wave_runner = AsyncAutonomousWaveRunner(
                runner=runner,
                throughput_policy=_throughput_policy(),
                staged_adapter=adapter,
            )
            wave_result = wave_runner.run_wave(
                wave=wave,
                feedback={
                    "operator_class": policy.operator_class,
                    "global_incumbent": policy.global_incumbent,
                    "conversion_threshold": policy.conversion_threshold,
                    "evidence_scope": "DEVELOPMENT_ONLY",
                    "scientific_authority": "NONE_SCHEDULING_ONLY",
                },
            )
            record = wave_result.throughput.records[0]
            commit = _candidate_commit(runner.repo_root, campaign_dir, candidate_id)
            candidate_delta = None
            candidate_score = None
            activation_admitted = False
            if commit is not None:
                candidate_profile, candidate_activation = _profile_pair(
                    runner=runner,
                    policy=policy,
                    candidate_id=candidate_id,
                    commit=commit,
                )
                profiles[candidate_id] = candidate_profile
                activations[candidate_id] = candidate_activation
                candidate_delta = compare_profiles(profiles[parent_id], candidate_profile)
                create_once_json(
                    campaign_dir
                    / "lineage_feedback"
                    / f"{candidate_id}.parent_delta.json",
                    candidate_delta,
                )
                activation_admitted = _activation_pass(candidate_activation, policy)
            l2 = record.decision(FunnelStage.L2)
            dev_valid = bool(
                l2
                and l2.mechanics_status is MechanicsStatus.PASS
                and l2.data_eligible
                and l2.controls
                and all(l2.controls.values())
            )
            if dev_valid and l2 is not None:
                candidate_score = float(l2.metrics[policy.incumbent_metric])
            improved = bool(
                activation_admitted
                and candidate_score is not None
                and candidate_score >= parent_score + policy.improvement_min_delta
            )
            attempt = AttemptFeedback(
                candidate_id=candidate_id,
                parent_candidate_id=parent_id,
                candidate_commit=commit,
                stages=_stage_feedback(record),
                terminal_stage=record.terminal_stage.value,
                terminal_status=record.terminal_status,
                error_type=record.error_type,
                error=record.error[:512] if record.error else None,
                local_incumbent_improved=improved,
            )
            wave_summary = {
                "wave_id": wave_id,
                "parent_candidate_id": parent_id,
                "candidate_id": candidate_id,
                "metrics": wave_result.throughput.metrics.model_dump(mode="json"),
                "resources": wave_result.throughput.resources,
            }
            create_once_json(
                decision_path,
                {
                    "attempt": attempt.model_dump(mode="json"),
                    "candidate_score": candidate_score,
                    "activation_admitted": activation_admitted,
                    "parent_child_delta": candidate_delta,
                    "wave_summary": wave_summary,
                },
            )
        attempts.append(attempt)
        last_delta = candidate_delta
        wave_summaries.append(wave_summary)
        if improved:
            if attempt.candidate_commit is None or candidate_score is None:
                raise ValueError("R3C improvement lacks a committed scored candidate")
            parent_id = candidate_id
            parent_score = float(candidate_score)
            runner._parent_commits[candidate_id] = attempt.candidate_commit  # noqa: SLF001
            consecutive_non_improving = 0
        else:
            consecutive_non_improving += 1

    supported = parent_score >= policy.conversion_threshold
    payload = {
        "schema_version": "1.0",
        "interpretation_status": (
            "ACTIVE_BASIN_QUALITY_CONVERSION_SUPPORTED"
            if supported
            else "ACTIVE_BASIN_QUALITY_CONVERSION_NOT_SUPPORTED"
        ),
        "interpretation_is_not_scientific_outcome": True,
        "active_basin": {
            "basin_id": basin.basin_id,
            "root_candidate_id": root_id,
            "root_score": basin.root_score,
            "best_descendant_id": parent_id,
            "best_descendant_score": parent_score,
            "conversion_threshold": policy.conversion_threshold,
            "threshold_met": supported,
            "root_activation": root_activation.model_dump(mode="json"),
            "root_exactness": root_canary,
        },
        "inactive_control_basins": {
            "status": "NOT_EVALUABLE_DATA",
            "reason": "MECHANISM_NOT_ACTIVATED_ON_FROZEN_DEVELOPMENT_INSTANCES",
            "used_for_conversion_claim": False,
        },
        "attempts": [item.model_dump(mode="json") for item in attempts],
        "scheduled_slots": len(attempts),
        "unspent_slots": policy.candidate_slots - len(attempts),
        "consecutive_non_improving": consecutive_non_improving,
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
        description="Run the development-only M2-R3C active-basin refinement repair"
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
        "EE_ALGOTUNE_WORKERS": str(min(4, available_cpu_count())),
    }.items():
        os.environ[key] = value
    payload = run(args.run_dir.resolve(), prepare_only=args.prepare_only)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
