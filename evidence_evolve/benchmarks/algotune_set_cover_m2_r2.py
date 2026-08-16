"""M2-R2 context-preserving asynchronous Set Cover development campaign.

The entrypoint imports only parent rights and development observations from the
completed M2-R1B controller-only run. It exposes no blind or confirmation path.
"""

from __future__ import annotations

import argparse
import hashlib
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
from evidence_evolve.discovery.m2_r2_escape import (
    M2R2AutonomousCampaignRunner,
    M2R2Policy,
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
from evidence_evolve.models import (
    Budgets,
    GateVerdict,
    MutationType,
    ObjectiveDirection,
)
from tasks.algotune_set_cover.staged_adapter import (
    SetCoverFunnelPolicy,
    SetCoverStagedAdapter,
    SetCoverStructuralTransitionAudit,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = REPO_ROOT / "runs/algotune_set_cover_m2_r2_dev_v1"
CONTEXT_ARM = REPO_ROOT / "runs/algotune_set_cover_m2_r1b_dev_v1/controller_only"
CONTEXT_RUN = CONTEXT_ARM / "campaign"
CONTEXT_EXECUTION_REPO = CONTEXT_ARM / "execution_repo"
POLICY_PATH = (
    REPO_ROOT
    / "research/policies/algotune_set_cover_m2_r2_context_escape_v0.yaml"
)
WAVE_ID = "R2-WAVE-001"
OPERATOR_DIRECTIVES = {
    "incumbent_local": (
        "Exploit the inherited incumbent with one bounded mechanism-level improvement; "
        "do not claim a structural root."
    ),
    "structural_rewrite": (
        "Replace both representation and solver process using decomposition, dynamic "
        "programming, or meet-in-the-middle; do not use residual-incidence pivot branching."
    ),
    "mechanism_substitution": (
        "Replace the incumbent core with relaxation, primal-dual, local-search, SAT/MaxSAT, "
        "or another non-branching paradigm; residual-incidence pivot branching is prohibited."
    ),
    "failure_directed": (
        "Target a concrete failure class in the frozen failure model while preserving the "
        "incumbent fallback; a single-path global greedy constructor is prohibited."
    ),
    "archive_mechanism_synthesis": (
        "Combine at least two distinct archived mechanisms from the failure model into one "
        "integrated solver; this is archive synthesis, not a claim of distinct root lineage."
    ),
    "context_radical": (
        "Use a solver paradigm absent from the supplied context while retaining a non-SEED "
        "implementation parent solely for interface and correctness knowledge."
    ),
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _prepare_execution_repo(run_root: Path) -> Path:
    destination = run_root / "execution_repo"
    manifest_path = run_root / "execution_repo_context.json"
    source_head = _git(CONTEXT_EXECUTION_REPO, "rev-parse", "HEAD")
    payload = {
        "schema_version": "1.0",
        "source": str(CONTEXT_EXECUTION_REPO.resolve()),
        "source_head": source_head,
        "context_contract_sha256": sha256_file(CONTEXT_RUN / "contract.locked.yaml"),
        "copy_preserves_context_candidate_commits": True,
    }
    if destination.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != payload:
            raise ValueError("M2-R2 execution context drift")
        return destination
    run_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CONTEXT_EXECUTION_REPO, destination, symlinks=True)
    _git(destination, "worktree", "prune")
    create_once_json(manifest_path, payload)
    return destination


def _load_policy() -> M2R2Policy:
    return M2R2Policy.model_validate(
        yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    )


def _operator_profile(slot_count: int) -> list[tuple[str, MutationType, DiscoveryMode]]:
    if slot_count == 4:
        return [
            ("structural_rewrite", MutationType.CROSS_FAMILY, DiscoveryMode.BREAKTHROUGH),
            ("mechanism_substitution", MutationType.CROSS_FAMILY, DiscoveryMode.BREAKTHROUGH),
            ("failure_directed", MutationType.FAILURE_DIRECTED, DiscoveryMode.BREAKTHROUGH),
            ("archive_mechanism_synthesis", MutationType.CROSS_FAMILY, DiscoveryMode.BREAKTHROUGH),
        ]
    if slot_count == 44:
        return [
            *[("incumbent_local", MutationType.MECHANISM, DiscoveryMode.NORMAL)] * 8,
            *[("structural_rewrite", MutationType.CROSS_FAMILY, DiscoveryMode.BREAKTHROUGH)] * 8,
            *[("mechanism_substitution", MutationType.CROSS_FAMILY, DiscoveryMode.BREAKTHROUGH)] * 8,
            *[("failure_directed", MutationType.FAILURE_DIRECTED, DiscoveryMode.BREAKTHROUGH)] * 8,
            *[("archive_mechanism_synthesis", MutationType.CROSS_FAMILY, DiscoveryMode.BREAKTHROUGH)] * 8,
            *[("context_radical", MutationType.CROSS_FAMILY, DiscoveryMode.BREAKTHROUGH)] * 4,
        ]
    raise ValueError("M2-R2 supports only the frozen 4-slot pilot or 44-slot wave")


def _runtime_contract(
    execution_repo: Path,
    run_root: Path,
    profile: list[tuple[str, MutationType, DiscoveryMode]],
) -> Any:
    path = run_root / "campaign_contract.locked.yaml"
    if path.is_file():
        return load_contract(path)
    template = execution_repo / benchmark.CONTRACT_TEMPLATE.relative_to(REPO_ROOT)
    contract = load_contract(template)
    structural = sum(mode is DiscoveryMode.BREAKTHROUGH for _name, _mutation, mode in profile)
    contract.campaign.id = f"algotune-set-cover-m2-r2-async-{len(profile)}"
    contract.campaign.base_commit = _git(execution_repo, "rev-parse", "HEAD")
    contract.campaign.claim_scope = "SET_COVER_M2_R2_DEVELOPMENT_ONLY"
    contract.budgets = Budgets(
        proposal_calls=len(profile) + structural,
        implementations=len(profile),
        mechanics_runs=len(profile),
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_contract(locked, path)
    return locked


def _context_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proposal_path in sorted(CONTEXT_RUN.glob("generations/*/proposals/*.json")):
        candidate_id = proposal_path.stem
        receipt_paths = sorted(
            (CONTEXT_RUN / "candidates" / candidate_id / "receipts").glob(
                "*.M0_MECHANICS.json"
            )
        )
        if not receipt_paths:
            continue
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_paths[0].read_text(encoding="utf-8"))["receipt"]
        evaluation = receipt["evaluation_input"]
        verdict = GateVerdict.model_validate(receipt["verdict"])
        item = CampaignCandidate.model_validate(proposal)
        commit = receipt.get("candidate_commit")
        code_sha256 = receipt.get("patch_sha256")
        metric = evaluation.get("metrics", {}).get("raw_speedup")
        if commit is None or code_sha256 is None or metric is None:
            continue
        rows.append(
            {
                "candidate_id": candidate_id,
                "item": item,
                "commit": str(commit),
                "code_sha256": str(code_sha256),
                "outcome": verdict.scientific_outcome,
                "disposition": search_disposition(verdict),
                "objective": float(metric),
                "protocol_valid": bool(verdict.protocol_valid),
                "data_eligible": bool(evaluation["data_eligible"]),
                "controls_pass": bool(evaluation["controls"])
                and all(bool(value) for value in evaluation["controls"].values()),
            }
        )
    return rows


def _import_context(
    runner: M2R2AutonomousCampaignRunner,
    policy: M2R2Policy,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    imported: list[dict[str, Any]] = []
    parent_families: dict[str, str] = {}
    for row in _context_rows():
        item = row["item"]
        candidate = item.acquisition.candidate
        parent_families[candidate.candidate_id] = candidate.family
        if not (
            row["protocol_valid"]
            and row["data_eligible"]
            and row["controls_pass"]
            and row["disposition"] in set(policy.code_parent_dispositions)
        ):
            continue
        _git(runner.repo_root, "cat-file", "-e", f"{row['commit']}^{{commit}}")
        duplicate = runner.population.claim_code(
            candidate_id=candidate.candidate_id,
            generation_id=f"CONTEXT-{candidate.candidate_id}",
            code_sha256=row["code_sha256"],
        )
        if duplicate is not None:
            raise ValueError(
                f"context candidate {candidate.candidate_id} duplicates {duplicate}"
            )
        runner.population.admit(
            candidate=candidate,
            generation_id=f"CONTEXT-{candidate.candidate_id}",
            candidate_commit=row["commit"],
            code_sha256=row["code_sha256"],
            search_disposition=row["disposition"],
            scientific_outcome=row["outcome"],
            acquisition_score=None,
            information_gain=item.acquisition.signals.information_gain,
            novelty=item.acquisition.signals.novelty,
            parent_dispositions=set(policy.code_parent_dispositions),
            stepping_stone_min_information_gain=policy.stepping_stone_min_information_gain,
            island_capacity=policy.island_capacity,
        )
        runner._parent_commits[candidate.candidate_id] = row["commit"]  # noqa: SLF001
        imported.append(row)
    if not imported:
        raise ValueError("M2-R2 imported no eligible development context parents")
    imported.sort(key=lambda row: (row["objective"], row["candidate_id"]), reverse=True)
    return imported, parent_families


def _root_lineages(rows: list[dict[str, Any]]) -> dict[str, str]:
    parents = {
        row["candidate_id"]: (
            row["item"].acquisition.candidate.genetic_parent_id
            or row["item"].acquisition.candidate.parent_ids[0]
        )
        for row in rows
    }
    roots: dict[str, str] = {"SEED": "SEED"}

    def root(candidate_id: str, seen: set[str]) -> str:
        if candidate_id in roots:
            return roots[candidate_id]
        if candidate_id in seen:
            raise ValueError(f"cycle in context lineage: {candidate_id}")
        parent = parents.get(candidate_id, "SEED")
        value = candidate_id if parent == "SEED" else root(parent, {*seen, candidate_id})
        roots[candidate_id] = value
        return value

    for candidate_id in parents:
        root(candidate_id, set())
    return roots


def _bind_wave_state(
    *,
    campaign_dir: Path,
    policy: M2R2Policy,
    imported: list[dict[str, Any]],
    parent_ids: list[str],
    profile: list[tuple[str, MutationType, DiscoveryMode]],
    incumbent: float,
) -> None:
    generation_dir = campaign_dir / "generations" / WAVE_ID
    structural = [entry for entry in profile if entry[2] is DiscoveryMode.BREAKTHROUGH]
    trace = M2ControllerTrace(
        generation_id=WAVE_ID,
        policy_id=policy.policy_id,
        mode=DiscoveryMode.BREAKTHROUGH,
        incumbent_metric=policy.incumbent_metric,
        incumbent_value_before=incumbent,
        stagnant_generations_before=policy.stagnation_generations,
        escape_budget_remaining_before=len(structural),
        escape_triggered=True,
        mutation_assignment=(structural[0][1] if structural else profile[0][1]),
        parent_pool=parent_ids,
        preferred_parent_ids=parent_ids,
        admitted_parent_ids=[row["candidate_id"] for row in imported],
        objective_values={row["candidate_id"]: row["objective"] for row in imported},
        root_lineages=_root_lineages(imported),
        required_structural_transition=bool(structural),
        required_seed_root=False,
    )
    state_path = generation_dir / "m2_controller_state.json"
    if state_path.exists():
        existing = M2ControllerTrace.model_validate_json(
            state_path.read_text(encoding="utf-8")
        )
        if existing != trace:
            raise ValueError("M2-R2 async controller state drift")
    else:
        create_once_json(state_path, trace)


def _wave_spec(
    profile: list[tuple[str, MutationType, DiscoveryMode]],
    parent_ids: list[str],
) -> AsyncWaveSpec:
    slots = []
    for index, (operator, mutation, mode) in enumerate(profile, start=1):
        primary = parent_ids[(index - 1) % len(parent_ids)]
        slots.append(
            AsyncWaveSlot(
                slot=index,
                dispatch_index=index,
                operator_class=operator,
                lineage_id=primary,
                island="main",
                eligible_parent_ids=[primary],
                primary_parent_id=primary,
                mutation=mutation,
                mode=mode,
                requires_structural_transition=(mode is DiscoveryMode.BREAKTHROUGH),
                operator_directive=OPERATOR_DIRECTIVES[operator],
            )
        )
    return AsyncWaveSpec(wave_id=WAVE_ID, slots=slots)


def _throughput_policy(
    profile: list[tuple[str, MutationType, DiscoveryMode]],
) -> ThroughputPolicy:
    count = len(profile)
    return ThroughputPolicy(
        policy_id=f"m2-r2-async-{count}-v0",
        total_candidate_budget=count,
        propose_workers=min(8, count),
        implement_workers=min(8, count),
        l0_workers=min(8, count),
        l1_workers=min(6, count),
        l2_workers=min(3, max(1, count // 2)),
        max_inflight_per_lineage=2,
        operator_quotas=dict(Counter(name for name, _mutation, _mode in profile)),
        shadow_audit_stride=4 if count == 4 else 8,
    )


def _manifest(
    run_root: Path,
    profile: list[tuple[str, MutationType, DiscoveryMode]],
) -> None:
    payload = {
        "schema_version": "1.0",
        "study_id": "algotune_set_cover_m2_r2_async_dev_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operator_profile": [
            {"operator": name, "mutation": mutation, "mode": mode}
            for name, mutation, mode in profile
        ],
        "context_run": str(CONTEXT_RUN.resolve()),
        "context_contract_sha256": sha256_file(CONTEXT_RUN / "contract.locked.yaml"),
        "policy_sha256": sha256_file(POLICY_PATH),
        "model": benchmark.MODEL,
        "reasoning_effort": benchmark.REASONING_EFFORT,
        "evidence_scope": "DEVELOPMENT_ONLY",
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
            raise ValueError("M2-R2 manifest drift")
    else:
        create_once_json(path, payload)


def run(run_root: Path, *, slot_count: int, prepare_only: bool) -> dict[str, Any]:
    profile = _operator_profile(slot_count)
    _manifest(run_root, profile)
    result_path = run_root / "result.json"
    if result_path.is_file() and not prepare_only:
        return json.loads(result_path.read_text(encoding="utf-8"))
    execution_repo = _prepare_execution_repo(run_root)
    contract = _runtime_contract(execution_repo, run_root, profile)
    policy = _load_policy()
    campaign_dir = run_root / "campaign"
    incumbent = float(
        json.loads((CONTEXT_ARM / "arm_result.json").read_text(encoding="utf-8"))[
            "development"
        ]["metrics"]["raw_speedup"]
    )
    runner = M2R2AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(execution_repo / contract.closure_registry),
        policy=policy.frozen_base_policy(),
        r2_policy=policy,
        context_run_dirs=[CONTEXT_RUN],
        repo_root=execution_repo,
        run_dir=campaign_dir,
        evaluate=benchmark.evaluate_ee,
        backend=benchmark._PinnedProotCodexBackend(),
        worktree_root=(
            Path(tempfile.gettempdir())
            / f"ee-algotune-m2-r2-{slot_count}-worktrees"
        ),
        reference_metrics={policy.incumbent_metric: incumbent},
        memory_enabled=True,
        timeout_seconds=1_200,
    )
    imported, parent_families = _import_context(runner, policy)
    tolerance = max(abs(incumbent), 1e-12) * policy.parent_quality_guardrail_fraction
    direction = contract.metrics.pareto_objectives[policy.incumbent_metric]
    guarded = [
        row
        for row in imported
        if (
            row["objective"] >= incumbent - tolerance
            if direction is ObjectiveDirection.MAXIMIZE
            else row["objective"] <= incumbent + tolerance
        )
    ]
    parent_rows = (guarded or imported)[: policy.parents_per_island]
    parent_ids = [row["candidate_id"] for row in parent_rows]
    _bind_wave_state(
        campaign_dir=campaign_dir,
        policy=policy,
        imported=imported,
        parent_ids=parent_ids,
        profile=profile,
        incumbent=incumbent,
    )
    wave = _wave_spec(profile, parent_ids)
    throughput_policy = _throughput_policy(profile)
    preparation = {
        "slot_count": slot_count,
        "proposal_budget": contract.budgets.proposal_calls,
        "implementation_budget": contract.budgets.implementations,
        "mechanics_budget": contract.budgets.mechanics_runs,
        "imported_context_candidates": len(imported),
        "eligible_parent_ids": parent_ids,
        "incumbent_speedup": incumbent,
        "probe_min_speedup": incumbent * 0.5,
        "wave_sha256": sha256_object(wave.model_dump(mode="json")),
        "throughput_policy": throughput_policy.model_dump(mode="json"),
        "evidence_scope": "DEVELOPMENT_ONLY",
        "blind_artifacts_read": False,
        "confirmation_runs": 0,
    }
    preparation_path = run_root / "preparation.json"
    if preparation_path.exists():
        if json.loads(preparation_path.read_text(encoding="utf-8")) != preparation:
            raise ValueError("M2-R2 preparation drift")
    else:
        create_once_json(preparation_path, preparation)
    if prepare_only:
        return preparation

    adapter = SetCoverStagedAdapter(
        contract=contract,
        policy=SetCoverFunnelPolicy(
            incumbent_speedup=incumbent,
            probe_min_speedup=incumbent * 0.5,
        ),
        structural_check=SetCoverStructuralTransitionAudit(
            repo_root=execution_repo,
            parent_families=parent_families,
            operator_plan_dir=(
                campaign_dir / "generations" / WAVE_ID / "r2_operator"
            ),
        ),
    )
    started = time.perf_counter()
    result = AsyncAutonomousWaveRunner(
        runner=runner,
        throughput_policy=throughput_policy,
        staged_adapter=adapter,
    ).run_wave(
        wave=wave,
        feedback={
            "incumbent_metric": policy.incumbent_metric,
            "incumbent_value": incumbent,
            "context_candidate_ids": [row["candidate_id"] for row in imported],
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
        "blind_artifacts_read": False,
        "blind_evaluator_calls": 0,
        "confirmation_runs": 0,
    }
    create_once_json(result_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the development-only M2-R2 asynchronous Set Cover wave"
    )
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--slots", type=int, choices=(4, 44), default=4)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    for key, value in {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "EE_ALGOTUNE_DEV_COUNT": "100",
        "EE_ALGOTUNE_DEV_REPEATS": "3",
        "EE_ALGOTUNE_WORKERS": "3",
    }.items():
        os.environ[key] = value
    payload = run(
        args.run_dir.resolve(),
        slot_count=args.slots,
        prepare_only=args.prepare_only,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
