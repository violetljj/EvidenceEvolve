"""Run the development-only M3-R0 planner research-taste comparison."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from evidence_evolve.artifacts import create_once_bytes, create_once_json
from evidence_evolve.benchmarks import algotune_blind as benchmark
from evidence_evolve.benchmarks import algotune_set_cover_m2_r4 as r4
from evidence_evolve.discovery.async_autonomous import (
    AsyncAutonomousWaveRunner,
    AsyncWaveSlot,
    AsyncWaveSpec,
)
from evidence_evolve.discovery.m2_escape import M2ControllerTrace
from evidence_evolve.discovery.m2_r4_structural import M2R4Policy
from evidence_evolve.discovery.m3_research_taste import (
    M3AutonomousCampaignRunner,
    M3CandidateObservation,
    M3ResearchTastePolicy,
    M3StructuralEscapeStagedAdapter,
    MechanismAncestryDetector,
    MechanismAncestryReport,
    M3StructuralEscapePlan,
    PlannerArm,
    load_m3_policy,
    score_research_taste,
    summarize_m3,
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
from evidence_evolve.models import Budgets, MutationType, ScientificOutcome
from tasks.algotune_set_cover.r4_profiling import evaluate_candidate_profiled
from tasks.algotune_set_cover.staged_adapter import (
    SetCoverFunnelPolicy,
    SetCoverProfiledStagedAdapter,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = REPO_ROOT / "runs/algotune_set_cover_m3_r0_dev_v7"
M3_POLICY_PATH = (
    REPO_ROOT / "research/policies/algotune_set_cover_m3_r0_research_taste_v0.yaml"
)
R4_POLICY_PATH = r4.POLICY_PATH
ROOT_REF = "refs/evidence-evolve/b1d5e0cf/R2-WAVE-001-C03"


def _profile_worker(connection: Any, candidate: str, seeds: list[int], repeats: int) -> None:
    try:
        connection.send(evaluate_candidate_profiled(candidate, seeds, repeats))
    except BaseException as exc:  # pragma: no cover - child-process containment
        connection.send(
            {
                "correct": False,
                "valid_rate": 0.0,
                "raw_speedup": 0.0,
                "telemetry_available": False,
                "failure": f"CHILD:{type(exc).__name__}:{exc}",
            }
        )
    finally:
        connection.close()


def evaluate_candidate_profiled_with_timeout(
    candidate: str | Path,
    seeds: Any,
    repeats: int,
    *,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Apply one frozen wall timeout; never retry or replace a timed-out sample."""

    seed_list = [int(seed) for seed in seeds]
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_profile_worker,
        args=(send, str(Path(candidate).resolve()), seed_list, int(repeats)),
    )
    started = time.perf_counter()
    process.start()
    send.close()
    try:
        if receive.poll(timeout_seconds):
            result = dict(receive.recv())
            process.join(timeout=1.0)
            result.setdefault("elapsed_seconds", time.perf_counter() - started)
            return result
        process.terminate()
        process.join(timeout=5.0)
        return {
            "raw_speedup": 0.0,
            "valid_rate": 0.0,
            "correct": False,
            "instance_count": len(seed_list),
            "elapsed_seconds": time.perf_counter() - started,
            "failure": f"TIMEOUT:{timeout_seconds:.3f}s",
            "telemetry_available": False,
            "adapter_exception": True,
            "worker_count": 1,
        }
    finally:
        receive.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5.0)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True
    ).stdout


def _load_policies() -> tuple[M3ResearchTastePolicy, M2R4Policy]:
    return (
        load_m3_policy(M3_POLICY_PATH),
        M2R4Policy.model_validate(yaml.safe_load(R4_POLICY_PATH.read_text())),
    )


def _prepare_execution_repo(arm_dir: Path) -> Path:
    source = r4.R3C_RUN_ROOT / "execution_repo"
    destination = arm_dir / "execution_repo"
    manifest = {
        "schema_version": "1.0",
        "source": str(source.resolve()),
        "source_head": _git(source, "rev-parse", "HEAD"),
        "runner_sha256": sha256_file(Path(__file__)),
        "m3_policy_sha256": sha256_file(M3_POLICY_PATH),
        "m3_code_sha256": sha256_file(
            REPO_ROOT / "evidence_evolve/discovery/m3_research_taste.py"
        ),
    }
    path = arm_dir / "execution_repo_context.json"
    if destination.exists():
        if json.loads(path.read_text()) != manifest:
            raise ValueError("M3 execution context drift")
        return destination
    arm_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    _git(destination, "worktree", "prune")
    create_once_json(path, manifest)
    return destination


def _contract(execution_repo: Path, arm_dir: Path, arm: PlannerArm, r4_policy: M2R4Policy):
    path = arm_dir / "campaign_contract.locked.yaml"
    if path.is_file():
        return load_contract(path)
    template = execution_repo / benchmark.CONTRACT_TEMPLATE.relative_to(REPO_ROOT)
    contract = load_contract(template)
    contract.campaign.id = f"algotune-set-cover-m3-r0-{arm.value.casefold()}"
    contract.campaign.base_commit = _git(execution_repo, "rev-parse", "HEAD")
    contract.campaign.claim_scope = "SET_COVER_M3_R0_DEVELOPMENT_ONLY"
    contract.budgets = Budgets(
        proposal_calls=16,
        implementations=8,
        mechanics_runs=8,
        replications=16,
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_contract(locked, path)
    return locked


def _wave(arm: PlannerArm, policy: M2R4Policy) -> AsyncWaveSpec:
    wave_id = f"M3-R0-{arm.value}"
    return AsyncWaveSpec(
        wave_id=wave_id,
        slots=[
            AsyncWaveSlot(
                slot=index,
                dispatch_index=index,
                operator_class=basin.basin_id,
                lineage_id=f"{arm.value}-{basin.basin_id}",
                island="main",
                eligible_parent_ids=[r4.ROOT_CANDIDATE_ID],
                primary_parent_id=r4.ROOT_CANDIDATE_ID,
                mutation=MutationType.CROSS_FAMILY,
                mode=DiscoveryMode.BREAKTHROUGH,
                requires_structural_transition=True,
                operator_directive=basin.directive,
            )
            for index, basin in enumerate(policy.basins, start=1)
        ],
    )


def _bind_state(campaign_dir: Path, wave: AsyncWaveSpec, policy: M2R4Policy, root: dict[str, Any]) -> None:
    trace = M2ControllerTrace(
        generation_id=wave.wave_id,
        policy_id=policy.policy_id,
        mode=DiscoveryMode.BREAKTHROUGH,
        incumbent_metric=policy.incumbent_metric,
        incumbent_value_before=root["objective"],
        stagnant_generations_before=policy.stagnation_generations,
        escape_budget_remaining_before=8,
        escape_triggered=True,
        mutation_assignment=MutationType.CROSS_FAMILY,
        parent_pool=[r4.ROOT_CANDIDATE_ID],
        preferred_parent_ids=[r4.ROOT_CANDIDATE_ID],
        admitted_parent_ids=[r4.ROOT_CANDIDATE_ID],
        objective_values={r4.ROOT_CANDIDATE_ID: root["objective"]},
        root_lineages={"SEED": "SEED", r4.ROOT_CANDIDATE_ID: r4.ROOT_CANDIDATE_ID},
        required_structural_transition=True,
        required_seed_root=False,
    )
    create_once_json(
        campaign_dir / "generations" / wave.wave_id / "m2_controller_state.json", trace
    )


def _throughput(arm: PlannerArm, policy: M2R4Policy) -> ThroughputPolicy:
    return ThroughputPolicy(
        policy_id=f"m3-r0-{arm.value.casefold()}-v0",
        total_candidate_budget=8,
        propose_workers=8,
        implement_workers=8,
        l0_workers=8,
        l1_workers=8,
        l2_workers=4,
        max_inflight_per_lineage=1,
        operator_quotas=dict(Counter(item.basin_id for item in policy.basins)),
        shadow_audit_stride=8,
    )


def _candidate_tokens(campaign_dir: Path, candidate_id: str) -> int:
    total = 0
    for path in campaign_dir.rglob("*.events.jsonl"):
        if candidate_id not in path.as_posix():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                usage = json.loads(line).get("usage", {})
            except (json.JSONDecodeError, AttributeError):
                continue
            total += int(usage.get("input_tokens", 0) or 0)
            total += int(usage.get("output_tokens", 0) or 0)
    return total


def _observations(
    arm: PlannerArm,
    wave: AsyncWaveSpec,
    result: Any,
    campaign_dir: Path,
    policy: M2R4Policy,
) -> list[M3CandidateObservation]:
    rows = []
    for record in result.throughput.records:
        slot = int(record.ticket.candidate_id.rsplit("-C", 1)[1])
        l0 = record.decision(FunnelStage.L0)
        terminal = record.decisions[-1] if record.decisions else None
        implemented = bool(
            record.terminal_stage is not FunnelStage.PROPOSE
            and not (
                record.terminal_stage is FunnelStage.IMPLEMENT
                and record.terminal_status == "FAILED"
            )
        )
        lineage = None
        exact = None
        if l0 is not None:
            lineage = not bool(l0.controls.get("closed_lineage_absent"))
            exact = bool(l0.controls.get("candidate_valid"))
        contract_path = (
            campaign_dir
            / "generations"
            / wave.wave_id
            / "m3_operator"
            / f"{record.ticket.candidate_id}.mechanism_contract.json"
        )
        contract_pass = None
        if arm is not PlannerArm.R4_BASELINE:
            contract_pass = (
                bool(json.loads(contract_path.read_text())["passed"])
                if contract_path.is_file()
                else False
            )
        speedups = [
            decision.metrics.get("raw_speedup")
            for decision in record.decisions
            if "raw_speedup" in decision.metrics
        ]
        rows.append(
            M3CandidateObservation(
                candidate_id=record.ticket.candidate_id,
                arm=arm,
                paired_slot=slot,
                proposal_contract_pass=contract_pass,
                implementation_succeeded=implemented,
                lineage_retained=lineage if implemented else None,
                exact_valid=exact if implemented else None,
                basin_signature=policy.basins[slot - 1].mechanism_signature,
                token_count=_candidate_tokens(campaign_dir, record.ticket.candidate_id),
                scientific_outcome=(
                    terminal.scientific_outcome
                    if terminal is not None
                    else ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER
                ),
                raw_speedup=max(speedups) if speedups else None,
            )
        )
    return rows


def run_arm(
    run_root: Path,
    arm: PlannerArm,
    m3_policy: M3ResearchTastePolicy,
    r4_policy: M2R4Policy,
    *,
    prepare_only: bool,
) -> dict[str, Any]:
    arm_dir = run_root / "arms" / arm.value.casefold()
    result_path = arm_dir / "result.json"
    if result_path.is_file() and not prepare_only:
        return json.loads(result_path.read_text())
    execution_repo = _prepare_execution_repo(arm_dir)
    contract = _contract(execution_repo, arm_dir, arm, r4_policy)
    campaign_dir = arm_dir / "campaign"
    runner = M3AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(execution_repo / contract.closure_registry),
        policy=r4_policy.frozen_base_policy(),
        r4_policy=r4_policy,
        m3_policy=m3_policy,
        planner_arm=arm,
        context_run_dirs=[r4.R2B_CAMPAIGN, r4.R3C_CAMPAIGN],
        repo_root=execution_repo,
        run_dir=campaign_dir,
        evaluate=benchmark.evaluate_ee,
        backend=benchmark._PinnedProotCodexBackend(),
        worktree_root=Path(tempfile.gettempdir()) / f"ee-algotune-m3-{arm.value.casefold()}",
        reference_metrics={r4_policy.incumbent_metric: r4._root_row()["objective"]},
        memory_enabled=True,
        timeout_seconds=1_800,
    )
    root = r4._import_root(runner, r4_policy)
    wave = _wave(arm, r4_policy)
    state_path = campaign_dir / "generations" / wave.wave_id / "m2_controller_state.json"
    if not state_path.is_file():
        _bind_state(campaign_dir, wave, r4_policy, root)
    closed_source = arm_dir / "closed_solver_reference.py"
    closed_bytes = _git_bytes(
        execution_repo,
        "show",
        f"{ROOT_REF}:tasks/algotune_set_cover/initial.py",
    )
    if not closed_source.is_file():
        create_once_bytes(closed_source, closed_bytes)
    detector = MechanismAncestryDetector(
        policy=m3_policy,
        closed_source=closed_source,
        report_dir=campaign_dir / "ancestry_reports",
    )
    throughput = _throughput(arm, r4_policy)
    preparation = {
        "arm": arm.value,
        "wave": wave.model_dump(mode="json"),
        "throughput": throughput.model_dump(mode="json"),
        "closed_source_sha256": sha256_file(closed_source),
        "same_development_evidence": m3_policy.source_result,
        "proposal_budget": 8,
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    preparation_path = arm_dir / "preparation.json"
    if not preparation_path.is_file():
        create_once_json(preparation_path, preparation)
    elif json.loads(preparation_path.read_text()) != preparation:
        raise ValueError("M3 arm preparation drift")
    if prepare_only:
        return preparation
    base_adapter = SetCoverProfiledStagedAdapter(
        contract=contract,
        policy=SetCoverFunnelPolicy(
            incumbent_speedup=r4_policy.conversion_threshold,
            probe_min_speedup=r4_policy.probe_min_speedup,
        ),
        evaluator=lambda candidate, seeds, repeats: evaluate_candidate_profiled_with_timeout(
            candidate,
            seeds,
            repeats,
            timeout_seconds=m3_policy.evaluator_timeout_seconds,
        ),
        structural_check=detector,
    )
    adapter = M3StructuralEscapeStagedAdapter(
        profiled_adapter=base_adapter, detector=detector
    )
    started = time.perf_counter()
    result = AsyncAutonomousWaveRunner(
        runner=runner, throughput_policy=throughput, staged_adapter=adapter
    ).run_wave(
        wave=wave,
        feedback={
            "closed_basin": r4_policy.closed_basin_id,
            "r3c_interpretation": r4_policy.closure_interpretation,
            "r4_interpretation": "STRUCTURAL_ESCAPE_NOT_DEMONSTRATED",
            "same_development_evidence": m3_policy.source_result,
        },
    )
    observations = _observations(arm, wave, result, campaign_dir, r4_policy)
    taste_scores = []
    if arm is PlannerArm.RESEARCH_TASTE:
        for observation in observations:
            plan_path = (
                campaign_dir
                / "generations"
                / wave.wave_id
                / "m3_operator"
                / f"{observation.candidate_id}.escape_plan.json"
            )
            ancestry_path = (
                campaign_dir
                / "ancestry_reports"
                / f"{observation.candidate_id}.mechanism_ancestry.json"
            )
            if not plan_path.is_file() or not ancestry_path.is_file():
                continue
            plan = M3StructuralEscapePlan.model_validate_json(plan_path.read_text())
            ancestry = MechanismAncestryReport.model_validate_json(
                ancestry_path.read_text()
            )
            taste_scores.append(
                score_research_taste(
                    plan.mechanism_first(),
                    ancestry,
                    prior_primitive_signatures=[
                        basin.mechanism_signature for basin in r4_policy.basins
                    ],
                ).model_dump(mode="json")
            )
    payload = {
        "schema_version": "1.0",
        "arm": arm.value,
        "wall_seconds": time.perf_counter() - started,
        "metrics": result.throughput.metrics.model_dump(mode="json"),
        "resources": result.throughput.resources,
        "observations": [item.model_dump(mode="json") for item in observations],
        "research_taste_scores": taste_scores,
        "tokens": benchmark._token_usage(campaign_dir),
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "conversion_claim_authorized": False,
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    create_once_json(result_path, payload)
    return payload


def run(run_root: Path, *, prepare_only: bool) -> dict[str, Any]:
    m3_policy, r4_policy = _load_policies()
    manifest = {
        "schema_version": "1.0",
        "study_id": m3_policy.study_id,
        "m3_policy_sha256": sha256_file(M3_POLICY_PATH),
        "r4_policy_sha256": sha256_file(R4_POLICY_PATH),
        "arm_order": [arm.value for arm in PlannerArm],
        "paired_basin_sha256": sha256_object(m3_policy.paired_basin_ids),
        "model": benchmark.MODEL,
        "reasoning_effort": benchmark.REASONING_EFFORT,
        "evidence_scope": "DEVELOPMENT_ONLY",
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    run_root.mkdir(parents=True, exist_ok=True)
    manifest_path = run_root / "manifest.json"
    if not manifest_path.is_file():
        create_once_json(manifest_path, manifest)
    elif json.loads(manifest_path.read_text()) != manifest:
        raise ValueError("M3 study manifest drift")
    arms = {
        arm.value: run_arm(
            run_root, arm, m3_policy, r4_policy, prepare_only=prepare_only
        )
        for arm in PlannerArm
    }
    if prepare_only:
        return {"manifest": manifest, "arms": arms}
    observations = [
        M3CandidateObservation.model_validate(row)
        for payload in arms.values()
        for row in payload["observations"]
    ]
    payload = {
        "schema_version": "1.0",
        "study_id": m3_policy.study_id,
        "primary_endpoint": m3_policy.primary_endpoint,
        "arm_summaries": [
            item.model_dump(mode="json")
            for item in summarize_m3(observations, m3_policy)
        ],
        "performance_is_secondary": True,
        "conversion_claim_authorized": False,
        "scientific_authority": "NONE_SCHEDULING_ONLY",
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    result_path = run_root / "result.json"
    if not result_path.is_file():
        create_once_json(result_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run M3-R0 research-taste admission")
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
    print(json.dumps(run(args.run_dir.resolve(), prepare_only=args.prepare_only), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
