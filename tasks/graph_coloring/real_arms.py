from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.backends.codex_cli import CodexCliBackend
from evidence_evolve.benchmarks.models import (
    ArmTrialSubmission,
    BenchmarkArm,
    BenchmarkTrialContext,
)
from evidence_evolve.discovery.autonomous import AutonomousCampaignRunner
from evidence_evolve.discovery.director import ResearchAction
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.gate_engine import GateEngine
from evidence_evolve.governance.protocol_lock import (
    ProtocolLock,
    dump_contract,
    load_contract,
)
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode, ResearchPolicyGenome
from evidence_evolve.models import Budgets, MutationType
from tasks.graph_coloring.autonomous_adapter import evaluate_candidate
from tasks.graph_coloring.campaign_evaluator import evaluate_development


CONTRACT_TEMPLATE = Path("research/contracts/graph_coloring_live_v0.template.yaml")
POLICY_PATH = Path("research/policies/graph_coloring_live_v0.yaml")
CANDIDATE_PATH = Path("tasks/graph_coloring/candidates/baseline.py")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def _source_snapshot(repo_root: Path) -> tuple[list[str], str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    paths = sorted(
        path
        for path in completed.stdout.split("\0")
        if path and not path.startswith("runs/")
    )
    unsafe = [path for path in paths if path.startswith(".evolve-worktrees/")]
    if unsafe:
        raise ValueError(f"runtime snapshot contains generated paths: {unsafe[:5]}")
    hashes = {path: sha256_file(repo_root / path) for path in paths}
    return paths, sha256_object(hashes)


def _prepare_execution_repo(context: BenchmarkTrialContext) -> Path:
    suite_run_dir = context.trial_dir.parents[2]
    execution_repo = suite_run_dir / "execution_repo"
    manifest_path = suite_run_dir / "execution_repo_snapshot.json"
    paths, snapshot_sha256 = _source_snapshot(context.repo_root)
    if execution_repo.exists():
        if not manifest_path.is_file():
            raise ValueError("execution repository exists without a snapshot manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("source_snapshot_sha256") != snapshot_sha256:
            raise ValueError("source worktree changed after benchmark snapshot creation")
        if manifest.get("execution_commit") != _git(execution_repo, "rev-parse", "HEAD"):
            raise ValueError("execution repository HEAD drifted")
        return execution_repo

    execution_repo.mkdir(parents=True)
    for relative in paths:
        source = context.repo_root / relative
        destination = execution_repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    _git(execution_repo, "init", "-b", "master")
    _git(execution_repo, "add", ".")
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=EvidenceEvolve Benchmark",
            "-c",
            "user.email=benchmark@invalid.local",
            "commit",
            "-m",
            "Frozen benchmark execution snapshot",
        ],
        cwd=execution_repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    execution_commit = _git(execution_repo, "rev-parse", "HEAD")
    create_once_json(
        manifest_path,
        {
            "source_snapshot_sha256": snapshot_sha256,
            "execution_commit": execution_commit,
            "file_count": len(paths),
            "authority": "LOCAL_BENCHMARK_EXECUTION_SNAPSHOT_ONLY",
        },
    )
    return execution_repo


def _codex_backend() -> CodexCliBackend:
    configured = os.environ.get("EVIDENCE_EVOLVE_CODEX_EXECUTABLE")
    candidates = [
        configured,
        str(Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"),
        str(Path.home() / ".codex" / ".sandbox-bin" / "codex.exe"),
        "codex",
    ]
    diagnostics: list[dict[str, object]] = []
    for candidate in candidates:
        if not candidate:
            continue
        backend = CodexCliBackend(candidate)
        status = backend.status()
        discovered = status.get("path")
        if os.name == "nt" and isinstance(discovered, str):
            executable_path = Path(discovered)
            if executable_path.is_file() and not (
                executable_path.parent / "codex-code-mode-host.exe"
            ).is_file():
                status = {
                    **status,
                    "usable": False,
                    "error": "CODE_MODE_HOST_NOT_COLOCATED",
                }
        diagnostics.append(status)
        if status.get("usable"):
            return backend
    raise RuntimeError(
        "no usable Codex CLI for live benchmark: "
        + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
    )


def _runtime_contract(
    context: BenchmarkTrialContext,
    execution_repo: Path,
) -> Any:
    contract = load_contract(execution_repo / CONTRACT_TEMPLATE)
    slug = {
        BenchmarkArm.VANILLA_CODEX: "v",
        BenchmarkArm.EVIDENCE_EVOLVE_NO_MEMORY: "nm",
        BenchmarkArm.EVIDENCE_EVOLVE_FULL: "full",
    }[context.arm]
    contract.campaign.id = f"gc-{slug}-{context.trial_seed}"
    contract.campaign.base_commit = _git(execution_repo, "rev-parse", "HEAD")
    limit = context.budget.candidate_evaluations_per_trial
    contract.budgets = Budgets(
        proposal_calls=limit,
        implementations=limit,
        mechanics_runs=limit,
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_benchmark_path = context.trial_dir / "campaign_contract.locked.yaml"
    if dump_benchmark_path.exists():
        existing = load_contract(dump_benchmark_path)
        if existing != locked:
            raise ValueError("runtime campaign contract drift")
    else:
        dump_contract(locked, dump_benchmark_path)
    return locked


def _policy(execution_repo: Path, arm: BenchmarkArm) -> ResearchPolicyGenome:
    payload = yaml.safe_load((execution_repo / POLICY_PATH).read_text(encoding="utf-8"))
    payload["policy_id"] = f"graph_coloring_{arm.value.lower()}_v0"
    return ResearchPolicyGenome.model_validate(payload)


def _token_usage(run_dir: Path) -> int:
    total = 0
    for path in run_dir.rglob("*.events.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") if isinstance(event, dict) else None
            if not isinstance(usage, dict):
                continue
            total += int(usage.get("input_tokens", 0) or 0)
            total += int(usage.get("output_tokens", 0) or 0)
    return total


def _worktree_root(context: BenchmarkTrialContext) -> Path:
    digest = hashlib.sha256(str(context.trial_dir).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "ee-gc-wt" / digest


def _write_failure_candidate(path: Path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = json.dumps(reason)
    path.write_text(
        "from __future__ import annotations\n\n"
        "def solve(node_count, edges, seed):\n"
        f"    raise RuntimeError({escaped})\n",
        encoding="utf-8",
    )


def _collect_candidate(
    *,
    source: Path | None,
    destination: Path,
    failure_reason: str,
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source is not None and source.is_file():
        shutil.copy2(source, destination)
    else:
        _write_failure_candidate(destination, failure_reason)
    return str(destination.resolve())


def _run_evidence_evolve(
    context: BenchmarkTrialContext,
    *,
    memory_enabled: bool,
) -> ArmTrialSubmission:
    execution_repo = _prepare_execution_repo(context)
    contract = _runtime_contract(context, execution_repo)
    backend = _codex_backend()
    arm_run_dir = context.trial_dir / "campaign"
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(
            execution_repo / contract.closure_registry
        ),
        policy=_policy(execution_repo, context.arm),
        repo_root=execution_repo,
        run_dir=arm_run_dir,
        evaluate=evaluate_candidate,
        backend=backend,
        worktree_root=_worktree_root(context),
        reference_metrics=evaluate_development(
            execution_repo / CANDIDATE_PATH, execution_repo
        )["metrics"],
        memory_enabled=memory_enabled,
        timeout_seconds=max(60, int(context.budget.wall_seconds_per_trial / 4)),
    )
    attempt_count = context.budget.candidate_evaluations_per_trial
    result = runner.run(
        generations=attempt_count,
        proposals_per_generation=1,
        max_evaluations_per_generation=1,
    )
    successful_ids = {
        evaluation.candidate_id
        for generation in result.generations
        for evaluation in generation.evaluations
    }
    candidate_paths: list[str] = []
    run_hash = hashlib.sha256(str(arm_run_dir.resolve()).encode("utf-8")).hexdigest()[:8]
    for index in range(1, attempt_count + 1):
        candidate_id = f"GEN-{index:03d}-C01"
        worktree_key = f"{contract.campaign.id}-{run_hash}-{candidate_id}"
        source = runner.worktrees.candidate_path(worktree_key) / CANDIDATE_PATH
        candidate_paths.append(
            _collect_candidate(
                source=(source if candidate_id in successful_ids else None),
                destination=context.trial_dir / "submitted_candidates" / f"C{index:03d}.py",
                failure_reason=f"EvidenceEvolve candidate failed: {candidate_id}",
            )
        )
    return ArmTrialSubmission(
        executor_id=(
            "evidence-evolve-full-v0"
            if memory_enabled
            else "evidence-evolve-no-memory-v0"
        ),
        candidate_paths=candidate_paths,
        proposal_calls_used=int(result.budgets["proposal_calls"]["used"]),
        token_count_used=_token_usage(arm_run_dir),
        metadata={
            "memory_enabled": str(memory_enabled).lower(),
            "execution_commit": contract.campaign.base_commit,
            "successful_candidates": str(len(successful_ids)),
        },
    )


def _run_vanilla(context: BenchmarkTrialContext) -> ArmTrialSubmission:
    execution_repo = _prepare_execution_repo(context)
    contract = _runtime_contract(context, execution_repo)
    backend = _codex_backend()
    arm_run_dir = context.trial_dir / "vanilla"
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(
            execution_repo / contract.closure_registry
        ),
        policy=_policy(execution_repo, context.arm),
        repo_root=execution_repo,
        run_dir=arm_run_dir,
        evaluate=evaluate_candidate,
        backend=backend,
        worktree_root=_worktree_root(context),
        reference_metrics=evaluate_development(
            execution_repo / CANDIDATE_PATH, execution_repo
        )["metrics"],
        memory_enabled=False,
        timeout_seconds=max(60, int(context.budget.wall_seconds_per_trial / 4)),
    )
    parent_id = "SEED"
    history: list[dict[str, object]] = []
    candidate_paths: list[str] = []
    mutations = [MutationType.MECHANISM, MutationType.REPRESENTATION]
    attempt_count = context.budget.candidate_evaluations_per_trial
    for index in range(1, attempt_count + 1):
        generation_id = f"VAN-{index:03d}"
        candidate_id = f"{generation_id}-C01"
        source: Path | None = None
        failure_reason = f"Vanilla Codex candidate failed: {candidate_id}"
        try:
            item = runner._propose_candidate(
                generation_id=generation_id,
                slot=1,
                island="vanilla",
                eligible_parents=[parent_id],
                feedback={"vanilla_iteration_history": history},
                required_mutation=mutations[(index - 1) % len(mutations)],
                research_action=ResearchAction.MUTATE,
                mode=DiscoveryMode.NORMAL,
            )
            runner.budgets.reserve(
                "mechanics_runs", 1, f"mechanics_runs:{generation_id}:{candidate_id}"
            )
            evaluation = runner._implement_and_evaluate(generation_id, item)
            verdict = GateEngine(contract).evaluate(evaluation.evaluation)
            run_hash = hashlib.sha256(
                str(arm_run_dir.resolve()).encode("utf-8")
            ).hexdigest()[:8]
            worktree_key = f"{contract.campaign.id}-{run_hash}-{candidate_id}"
            source = runner.worktrees.candidate_path(worktree_key) / CANDIDATE_PATH
            history.append(
                {
                    "candidate_id": candidate_id,
                    "metrics": evaluation.evaluation.metrics,
                    "controls": evaluation.evaluation.controls,
                    "gate_decision": verdict.decision.value,
                }
            )
            if (
                verdict.scientific_outcome.value == "POSITIVE_HEADROOM"
                and evaluation.candidate_commit is not None
            ):
                runner._parent_commits[candidate_id] = evaluation.candidate_commit
                parent_id = candidate_id
        except Exception as exc:
            failure_reason = f"{failure_reason}: {type(exc).__name__}: {exc}"
        candidate_paths.append(
            _collect_candidate(
                source=source,
                destination=context.trial_dir / "submitted_candidates" / f"C{index:03d}.py",
                failure_reason=failure_reason,
            )
        )
    return ArmTrialSubmission(
        executor_id="vanilla-codex-governed-iteration-v0",
        candidate_paths=candidate_paths,
        proposal_calls_used=int(runner.budgets.snapshot()["proposal_calls"]["used"]),
        token_count_used=_token_usage(arm_run_dir),
        metadata={
            "memory_enabled": "false",
            "population_feedback": "false",
            "research_director": "false",
            "successful_candidates": str(len(history)),
        },
    )


def run_three_arm_trial(context: BenchmarkTrialContext) -> ArmTrialSubmission:
    if context.arm == BenchmarkArm.VANILLA_CODEX:
        return _run_vanilla(context)
    if context.arm == BenchmarkArm.EVIDENCE_EVOLVE_NO_MEMORY:
        return _run_evidence_evolve(context, memory_enabled=False)
    if context.arm == BenchmarkArm.EVIDENCE_EVOLVE_FULL:
        return _run_evidence_evolve(context, memory_enabled=True)
    raise ValueError(f"unsupported benchmark arm: {context.arm}")
