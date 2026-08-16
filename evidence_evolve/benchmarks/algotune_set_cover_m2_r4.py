"""Run the development-only M2-R4 eight-root Structural Basin Wave."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter
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
from evidence_evolve.discovery.m2_r4_structural import (
    M2R4AutonomousCampaignRunner,
    M2R4Policy,
    R4SourceAudit,
)
from evidence_evolve.discovery.throughput import ThroughputPolicy
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import (
    ProtocolLock,
    dump_contract,
    load_contract,
)
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode
from evidence_evolve.models import Budgets, GateVerdict, MutationType
from tasks.algotune_set_cover.r4_profiling import evaluate_candidate_profiled
from tasks.algotune_set_cover.staged_adapter import (
    SetCoverFunnelPolicy,
    SetCoverProfiledStagedAdapter,
    SetCoverStructuralTransitionAudit,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = REPO_ROOT / "runs/algotune_set_cover_m2_r4_dev_v3"
R3C_RUN_ROOT = REPO_ROOT / "runs/algotune_set_cover_m2_r3c_dev_v1"
R3C_CAMPAIGN = R3C_RUN_ROOT / "campaign"
R2B_CAMPAIGN = REPO_ROOT / "runs/algotune_set_cover_m2_r2b_pilot_dev_v1/campaign"
ROOT_CANDIDATE_ID = "R2-WAVE-001-C03"
WAVE_ID = "M2-R4-STRUCTURAL-001"
POLICY_PATH = (
    REPO_ROOT
    / "research/policies/algotune_set_cover_m2_r4_structural_basin_wave_v0.yaml"
)
R3C_CLOSURE_PATH = (
    REPO_ROOT
    / "research/results/algotune_set_cover_m2_r3c_closure_v0/result.json"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _load_policy() -> M2R4Policy:
    return M2R4Policy.model_validate(
        yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    )


def _prepare_execution_repo(run_root: Path) -> Path:
    source = R3C_RUN_ROOT / "execution_repo"
    destination = run_root / "execution_repo"
    payload = {
        "schema_version": "1.0",
        "source": str(source.resolve()),
        "source_head": _git(source, "rev-parse", "HEAD"),
        "r3c_result_sha256": sha256_file(R3C_RUN_ROOT / "result.json"),
        "runner_sha256": sha256_file(Path(__file__)),
        "structural_policy_code_sha256": sha256_file(
            REPO_ROOT / "evidence_evolve/discovery/m2_r4_structural.py"
        ),
        "profiled_adapter_sha256": sha256_file(
            REPO_ROOT / "tasks/algotune_set_cover/staged_adapter.py"
        ),
        "profiling_evaluator_sha256": sha256_file(
            REPO_ROOT / "tasks/algotune_set_cover/r4_profiling.py"
        ),
        "copy_preserves_context_candidate_commits": True,
    }
    manifest_path = run_root / "execution_repo_context.json"
    if destination.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != payload:
            raise ValueError("M2-R4 execution context drift")
        return destination
    run_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    _git(destination, "worktree", "prune")
    create_once_json(manifest_path, payload)
    return destination


def _runtime_contract(execution_repo: Path, run_root: Path, policy: M2R4Policy) -> Any:
    path = run_root / "campaign_contract.locked.yaml"
    if path.is_file():
        return load_contract(path)
    template = execution_repo / benchmark.CONTRACT_TEMPLATE.relative_to(REPO_ROOT)
    contract = load_contract(template)
    contract.campaign.id = "algotune-set-cover-m2-r4-structural-basin-wave-8"
    contract.campaign.base_commit = _git(execution_repo, "rev-parse", "HEAD")
    contract.campaign.claim_scope = "SET_COVER_M2_R4_DEVELOPMENT_ONLY"
    contract.budgets = Budgets(
        proposal_calls=policy.candidate_slots * 2,
        implementations=policy.candidate_slots,
        mechanics_runs=policy.candidate_slots,
        replications=policy.candidate_slots * 2,
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_contract(locked, path)
    return locked


def _root_row() -> dict[str, Any]:
    proposal_path = next(
        R2B_CAMPAIGN.glob(f"generations/*/proposals/{ROOT_CANDIDATE_ID}.json")
    )
    receipt_path = next(
        (R2B_CAMPAIGN / "candidates" / ROOT_CANDIDATE_ID / "receipts").glob(
            "*.M0_MECHANICS.json"
        )
    )
    item = CampaignCandidate.model_validate_json(proposal_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))["receipt"]
    verdict = GateVerdict.model_validate(receipt["verdict"])
    evaluation = receipt["evaluation_input"]
    return {
        "item": item,
        "candidate_id": ROOT_CANDIDATE_ID,
        "commit": str(receipt["candidate_commit"]),
        "code_sha256": str(receipt["patch_sha256"]),
        "objective": float(evaluation["metrics"]["raw_speedup"]),
        "outcome": verdict.scientific_outcome,
        "disposition": search_disposition(verdict),
    }


def _import_root(runner: M2R4AutonomousCampaignRunner, policy: M2R4Policy) -> dict[str, Any]:
    row = _root_row()
    candidate = row["item"].acquisition.candidate
    _git(runner.repo_root, "cat-file", "-e", f"{row['commit']}^{{commit}}")
    duplicate = runner.population.claim_code(
        candidate_id=ROOT_CANDIDATE_ID,
        generation_id=f"CONTEXT-{ROOT_CANDIDATE_ID}",
        code_sha256=row["code_sha256"],
    )
    if duplicate is not None:
        raise ValueError(f"R4 root duplicates {duplicate}")
    runner.population.admit(
        candidate=candidate,
        generation_id=f"CONTEXT-{ROOT_CANDIDATE_ID}",
        candidate_commit=row["commit"],
        code_sha256=row["code_sha256"],
        search_disposition=row["disposition"],
        scientific_outcome=row["outcome"],
        acquisition_score=None,
        information_gain=row["item"].acquisition.signals.information_gain,
        novelty=row["item"].acquisition.signals.novelty,
        parent_dispositions=set(policy.code_parent_dispositions),
        stepping_stone_min_information_gain=policy.stepping_stone_min_information_gain,
        island_capacity=policy.island_capacity,
    )
    runner._parent_commits[ROOT_CANDIDATE_ID] = row["commit"]  # noqa: SLF001
    return row


def _bind_wave_state(
    campaign_dir: Path, policy: M2R4Policy, root: dict[str, Any]
) -> None:
    trace = M2ControllerTrace(
        generation_id=WAVE_ID,
        policy_id=policy.policy_id,
        mode=DiscoveryMode.BREAKTHROUGH,
        incumbent_metric=policy.incumbent_metric,
        incumbent_value_before=root["objective"],
        stagnant_generations_before=policy.stagnation_generations,
        escape_budget_remaining_before=policy.candidate_slots,
        escape_triggered=True,
        mutation_assignment=MutationType.CROSS_FAMILY,
        parent_pool=[ROOT_CANDIDATE_ID],
        preferred_parent_ids=[ROOT_CANDIDATE_ID],
        admitted_parent_ids=[ROOT_CANDIDATE_ID],
        objective_values={ROOT_CANDIDATE_ID: root["objective"]},
        root_lineages={"SEED": "SEED", ROOT_CANDIDATE_ID: ROOT_CANDIDATE_ID},
        required_structural_transition=True,
        required_seed_root=False,
    )
    path = campaign_dir / "generations" / WAVE_ID / "m2_controller_state.json"
    if path.is_file():
        if M2ControllerTrace.model_validate_json(path.read_text(encoding="utf-8")) != trace:
            raise ValueError("M2-R4 controller state drift")
    else:
        create_once_json(path, trace)


def _wave(policy: M2R4Policy) -> AsyncWaveSpec:
    slots = [
        AsyncWaveSlot(
            slot=index,
            dispatch_index=index,
            operator_class=basin.basin_id,
            lineage_id=f"R4-{basin.basin_id}",
            island="main",
            eligible_parent_ids=[ROOT_CANDIDATE_ID],
            primary_parent_id=ROOT_CANDIDATE_ID,
            mutation=MutationType.CROSS_FAMILY,
            mode=DiscoveryMode.BREAKTHROUGH,
            requires_structural_transition=True,
            operator_directive=basin.directive,
        )
        for index, basin in enumerate(policy.basins, start=1)
    ]
    return AsyncWaveSpec(wave_id=WAVE_ID, slots=slots)


def _throughput(policy: M2R4Policy) -> ThroughputPolicy:
    return ThroughputPolicy(
        policy_id="m2-r4-structural-basin-wave-8-v0",
        total_candidate_budget=policy.candidate_slots,
        propose_workers=8,
        implement_workers=8,
        l0_workers=8,
        l1_workers=8,
        l2_workers=4,
        max_inflight_per_lineage=1,
        operator_quotas=dict(Counter(basin.basin_id for basin in policy.basins)),
        shadow_audit_stride=8,
    )


def _manifest(run_root: Path, policy: M2R4Policy, wave: AsyncWaveSpec) -> None:
    payload = {
        "schema_version": "1.0",
        "study_id": "algotune_set_cover_m2_r4_structural_basin_wave_v0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy_sha256": sha256_file(POLICY_PATH),
        "r3c_closure_sha256": sha256_file(R3C_CLOSURE_PATH),
        "r3c_result_sha256": sha256_file(R3C_RUN_ROOT / "result.json"),
        "wave_sha256": sha256_object(wave.model_dump(mode="json")),
        "model": benchmark.MODEL,
        "reasoning_effort": benchmark.REASONING_EFFORT,
        "evidence_scope": "DEVELOPMENT_ONLY",
        "blind_artifacts_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
        "conversion_threshold_unchanged": policy.conversion_threshold,
    }
    path = run_root / "manifest.json"
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if {k: v for k, v in existing.items() if k != "created_at"} != {
            k: v for k, v in payload.items() if k != "created_at"
        }:
            raise ValueError("M2-R4 manifest drift")
    else:
        create_once_json(path, payload)


def run(run_root: Path, *, prepare_only: bool) -> dict[str, Any]:
    policy = _load_policy()
    wave = _wave(policy)
    _manifest(run_root, policy, wave)
    result_path = run_root / "result.json"
    if result_path.is_file() and not prepare_only:
        return json.loads(result_path.read_text(encoding="utf-8"))
    execution_repo = _prepare_execution_repo(run_root)
    contract = _runtime_contract(execution_repo, run_root, policy)
    campaign_dir = run_root / "campaign"
    runner = M2R4AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(execution_repo / contract.closure_registry),
        policy=policy.frozen_base_policy(),
        r4_policy=policy,
        context_run_dirs=[R2B_CAMPAIGN, R3C_CAMPAIGN],
        repo_root=execution_repo,
        run_dir=campaign_dir,
        evaluate=benchmark.evaluate_ee,
        backend=benchmark._PinnedProotCodexBackend(),
        worktree_root=Path(tempfile.gettempdir()) / "ee-algotune-m2-r4-worktrees",
        reference_metrics={policy.incumbent_metric: _root_row()["objective"]},
        memory_enabled=True,
        timeout_seconds=1_800,
    )
    root = _import_root(runner, policy)
    _bind_wave_state(campaign_dir, policy, root)
    throughput = _throughput(policy)
    preparation = {
        "candidate_slots": policy.candidate_slots,
        "basin_ids": [basin.basin_id for basin in policy.basins],
        "mechanism_signatures": {
            basin.basin_id: basin.mechanism_signature for basin in policy.basins
        },
        "root_candidate_id": ROOT_CANDIDATE_ID,
        "root_speedup": root["objective"],
        "probe_min_speedup": policy.probe_min_speedup,
        "conversion_threshold": policy.conversion_threshold,
        "throughput_policy": throughput.model_dump(mode="json"),
        "evidence_scope": "DEVELOPMENT_ONLY",
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    preparation_path = run_root / "preparation.json"
    if preparation_path.is_file():
        if json.loads(preparation_path.read_text(encoding="utf-8")) != preparation:
            raise ValueError("M2-R4 preparation drift")
    else:
        create_once_json(preparation_path, preparation)
    if prepare_only:
        return preparation

    base_audit = SetCoverStructuralTransitionAudit(
        repo_root=execution_repo,
        parent_families={ROOT_CANDIDATE_ID: root["item"].acquisition.candidate.family},
        operator_plan_dir=campaign_dir / "generations" / WAVE_ID / "r4_operator",
    )
    audit = R4SourceAudit(
        base_audit=base_audit,
        policy=policy,
        operator_plan_dir=campaign_dir / "generations" / WAVE_ID / "r4_operator",
    )
    adapter = SetCoverProfiledStagedAdapter(
        contract=contract,
        policy=SetCoverFunnelPolicy(
            incumbent_speedup=policy.conversion_threshold,
            probe_min_speedup=policy.probe_min_speedup,
        ),
        evaluator=evaluate_candidate_profiled,
        structural_check=audit,
    )
    started = time.perf_counter()
    result = AsyncAutonomousWaveRunner(
        runner=runner,
        throughput_policy=throughput,
        staged_adapter=adapter,
    ).run_wave(
        wave=wave,
        feedback={
            "closed_basin": policy.closed_basin_id,
            "closure_interpretation": policy.closure_interpretation,
            "incumbent_value": root["objective"],
            "probe_min_speedup": policy.probe_min_speedup,
            "conversion_threshold": policy.conversion_threshold,
            "evidence_scope": "DEVELOPMENT_ONLY",
        },
    )
    payload = {
        "schema_version": "1.0",
        "wave_id": WAVE_ID,
        "wall_seconds": time.perf_counter() - started,
        "metrics": result.throughput.metrics.model_dump(mode="json"),
        "resources": result.throughput.resources,
        "receipt_paths": result.receipt_paths,
        "tokens": benchmark._token_usage(campaign_dir),
        "conversion_threshold": policy.conversion_threshold,
        "conversion_claim_authorized": False,
        "blind_artifacts_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
        "scientific_authority": "NONE_SCHEDULING_ONLY",
    }
    create_once_json(result_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run M2-R4 development-only Structural Basin Wave"
    )
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    for key, value in {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "EE_ALGOTUNE_WORKERS": "1",
    }.items():
        os.environ[key] = value
    payload = run(args.run_dir.resolve(), prepare_only=args.prepare_only)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
