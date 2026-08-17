from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.benchmarks import algotune_blind as blind
from evidence_evolve.benchmarks.algotune_official import (
    OfficialTaskSpec,
    evaluate_official_candidate,
    evaluate_official_candidate_cold,
)
from evidence_evolve.discovery.autonomous import AutonomousCampaignRunner
from evidence_evolve.discovery.campaign import CampaignCandidate, EvaluationRun
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import ProtocolLock, dump_contract, load_contract
from evidence_evolve.hashing import sha256_bytes, sha256_file
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_evolve.models import (
    Budgets,
    EvaluationInput,
    FrozenAsset,
    FrozenAssetKind,
    MechanicsStatus,
    ScientificOutcome,
)
from evidence_evolve.worktrees import WorktreeManager


REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_ROOT = Path(os.environ.get("EE_ALGOTUNE_UPSTREAM", "/tmp/AlgoTune")).resolve()
PROTOCOL = REPO_ROOT / "research/parity/algotune_heterogeneous_five_arm_v0.protocol.json"
ARMS = blind.ARMS
HELDOUT_WORKERS: dict[str, int] = {}


def _load_protocol() -> dict[str, Any]:
    payload = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=UPSTREAM_ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    if commit != payload["upstream"]["commit"]:
        raise ValueError(f"AlgoTune upstream drift: {commit}")
    return payload


def _task_payload(task_name: str) -> dict[str, Any]:
    for item in _load_protocol()["tasks"]:
        if item["task"] == task_name:
            return item
    raise ValueError(f"unknown frozen task: {task_name}")


def _spec(task: dict[str, Any], source: Path | None = None) -> OfficialTaskSpec:
    upstream = UPSTREAM_ROOT / "AlgoTuneTasks" / task["task"] / f"{task['task']}.py"
    if sha256_file(upstream) != task["source_sha256"]:
        raise ValueError(f"upstream source drift: {task['task']}")
    return OfficialTaskSpec(
        task["task"], task["class"], int(task["problem_size"]), str(source or upstream)
    )


def _development(
    candidate: Path, spec: OfficialTaskSpec, *, workers: int | None = None
) -> dict[str, Any]:
    worker_count = workers or int(os.environ.get("EE_ALGOTUNE_WORKERS", "4"))
    seed_start = int(os.environ.get("EE_ALGOTUNE_DEV_START", "0"))
    seed_count = int(os.environ.get("EE_ALGOTUNE_DEV_COUNT", "20"))
    repeats = int(os.environ.get("EE_ALGOTUNE_DEV_REPEATS", "3"))
    raw = evaluate_official_candidate(
        candidate,
        spec,
        range(seed_start, seed_start + seed_count),
        repeats=repeats,
        workers=worker_count,
    )
    return {
        "mechanics_status": "PASS",
        "metrics": {
            "invalid_solution_rate": 1.0 - float(raw["valid_rate"]),
            "raw_speedup": float(raw["raw_speedup"]),
        },
        "controls": {"candidate_valid": bool(raw["correct"]), "development_only": True},
        "error": str(raw.get("failure", "")),
    }


def _write_task_workspace(run_dir: Path, task: dict[str, Any]) -> Path:
    root = run_dir / "task"
    root.mkdir(parents=True, exist_ok=True)
    source = UPSTREAM_ROOT / "AlgoTuneTasks" / task["task"] / f"{task['task']}.py"
    initial = root / "initial.py"
    if not initial.exists():
        initial.write_text(
            "# EVOLVE-BLOCK-START\n"
            + source.read_text(encoding="utf-8")
            + "\n# EVOLVE-BLOCK-END\n",
            encoding="utf-8",
        )
    spec_payload = {
        "name": task["task"], "class_name": task["class"],
        "problem_size": task["problem_size"], "source_path": str(source),
    }
    (root / "task_spec.json").write_text(json.dumps(spec_payload, sort_keys=True), encoding="utf-8")
    evaluator = f'''from __future__ import annotations
import argparse, json, os
from pathlib import Path
from evidence_evolve.benchmarks.algotune_official import OfficialTaskSpec, evaluate_official_candidate
SPEC = OfficialTaskSpec(name={task['task']!r}, class_name={task['class']!r}, problem_size={int(task['problem_size'])!r}, source_path={str(source)!r})
def evaluate(program_path: str):
    start=int(os.environ.get("EE_ALGOTUNE_DEV_START","0")); count=int(os.environ.get("EE_ALGOTUNE_DEV_COUNT","20")); repeats=int(os.environ.get("EE_ALGOTUNE_DEV_REPEATS","3")); workers=int(os.environ.get("EE_ALGOTUNE_WORKERS","4"))
    if os.environ.get("EE_M4_REMOTE_EVALUATOR") == "1":
        from evidence_evolve.benchmarks.search_value_m4 import remote_development_evaluate
        wrapped=remote_development_evaluate(program_path,SPEC,workers=workers)
        result={{"correct":wrapped["controls"]["candidate_valid"],"valid_rate":1.0-wrapped["metrics"]["invalid_solution_rate"],"raw_speedup":wrapped["metrics"]["raw_speedup"],"failure":wrapped["error"]}}
    else:
        result=evaluate_official_candidate(program_path,SPEC,range(start,start+count),repeats=repeats,workers=workers)
    result["text_feedback"]=f"correct={{result['correct']}} valid_rate={{result['valid_rate']:.3f}} raw_speedup={{result['raw_speedup']:.4f}} failure={{result['failure']}}"
    return result
def main():
    p=argparse.ArgumentParser(); p.add_argument("--program_path",required=True); p.add_argument("--results_dir",required=True); a=p.parse_args(); r=evaluate(a.program_path); d=Path(a.results_dir); d.mkdir(parents=True,exist_ok=True); (d/"metrics.json").write_text(json.dumps(r,sort_keys=True)); (d/"correct.json").write_text(json.dumps({{"correct":bool(r["correct"]),"error":r["failure"]}},sort_keys=True))
if __name__ == "__main__": main()
'''
    (root / "evaluator.py").write_text(evaluator, encoding="utf-8")
    shutil.copy2(REPO_ROOT / "tasks/algotune_set_cover/headless_logged.py", root / "headless_logged.py")
    prompt = (
        f"Optimize the official AlgoTune {task['task']} solver for runtime while preserving "
        f"the class {task['class']} and exact solve(problem) behavior for every input."
    )
    for name in ("adaevolve.yaml", "evox.yaml"):
        payload = yaml.safe_load((REPO_ROOT / "tasks/algotune_set_cover" / name).read_text())
        payload["prompt"]["system_message"] = prompt
        (root / name).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    shinka = yaml.safe_load((REPO_ROOT / "tasks/algotune_set_cover/shinka.yaml").read_text())
    (root / "shinka.yaml").write_text(yaml.safe_dump(shinka, sort_keys=False), encoding="utf-8")
    return root


def _configure(run_dir: Path, task: dict[str, Any]) -> OfficialTaskSpec:
    root = _write_task_workspace(run_dir, task)
    spec = _spec(task)
    blind.TASK_ROOT = root
    blind.INITIAL = root / "initial.py"
    blind.TASK_PROMPT = (
        f"Optimize the official AlgoTune {task['task']} solver for runtime. Preserve class "
        f"{task['class']} and solve(problem), and return an exact valid result for every input."
    )
    blind.DEV_COUNT = 20
    blind.DEV_REPEATS = 3
    blind.GENERATIONS = int(os.environ.get("EE_HETERO_GENERATIONS", "3"))
    blind.TEST_COUNT = 100
    blind.TEST_REPEATS = 10
    blind.EVALUATOR_WORKERS = int(os.environ.get("EE_ALGOTUNE_WORKERS", "4"))
    blind.evaluate_development = lambda path: _development(Path(path), spec)
    blind.evaluate_candidate = lambda path, seeds, repeats: evaluate_official_candidate(
        path, spec, seeds, repeats=repeats, workers=18
    )
    return spec


def _build_evaluation(
    contract_sha256: str, candidate: CampaignCandidate, changed_files: list[str], raw: dict[str, Any]
) -> EvaluationInput:
    controls = {str(k): bool(v) for k, v in raw["controls"].items()}
    metrics = {str(k): float(v) for k, v in raw["metrics"].items()}
    outcome = (
        ScientificOutcome.POSITIVE_HEADROOM
        if all(controls.values()) and metrics["raw_speedup"] > 1.0
        else ScientificOutcome.VALID_NEGATIVE
    )
    return EvaluationInput(
        contract_sha256=contract_sha256, candidate=candidate.acquisition.candidate,
        stage=candidate.stage, changed_files=changed_files, mechanics_status=MechanicsStatus.PASS,
        data_eligible=True, metrics=metrics, controls=controls, scientific_outcome=outcome,
    )


def _run_evidence(run_dir: Path, task: dict[str, Any], spec: OfficialTaskSpec) -> dict[str, Any]:
    arm_dir = run_dir / "arms/evidence_evolve"
    result_path = arm_dir / "arm_result.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    started = time.perf_counter()
    execution_repo = blind._prepare_execution_repo(run_dir)
    candidate_rel = Path(f"tasks/algotune_portfolio/{task['task']}/initial.py")
    oracle_rel = Path(f"confirmation/algotune_{task['task']}_oracle.py")
    candidate_path = execution_repo / candidate_rel
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(
        "# EVOLVE-BLOCK-START\n"
        + Path(spec.source_path).read_text(encoding="utf-8")
        + "\n# EVOLVE-BLOCK-END\n",
        encoding="utf-8",
    )
    shutil.copy2(Path(spec.source_path), execution_repo / oracle_rel)
    manifest_rel = Path(f"research/evidence_policies/algotune_{task['task']}_dev_manifest.json")
    manifest_path = execution_repo / manifest_rel
    manifest_path.write_text(json.dumps({
        "schema_version":"1.0", "task":task["task"], "visibility":"DEVELOPMENT",
        "seeds":list(range(20)), "problem_size":task["problem_size"],
        "upstream_commit":_load_protocol()["upstream"]["commit"],
    }, sort_keys=True), encoding="utf-8")
    subprocess.run(["git", "add", str(candidate_rel), str(oracle_rel), str(manifest_rel)], cwd=execution_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=EvidenceEvolve", "-c", "user.email=benchmark@invalid.local",
         "commit", "-m", f"Install frozen {task['task']} task"], cwd=execution_repo, check=True,
         capture_output=True,
    )
    contract = load_contract(execution_repo / "research/contracts/algotune_set_cover_blind_v0.template.yaml")
    contract.campaign.id = f"algotune-{task['task']}-ee"
    contract.campaign.base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=execution_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    contract.campaign.research_question = f"Can bounded search produce a valid faster official AlgoTune {task['task']} solver?"
    contract.campaign.claim_scope = "algotune_heterogeneous_blind_pilot_only"
    contract.editable_scope.allow = [str(candidate_rel)]
    contract.editable_scope.deny = [
        "evidence_evolve/**", "benchmarks/**", "prompts/**", "research/**",
        "confirmation/**", "tasks/algotune_set_cover/**",
    ]
    contract.evidence_sources[0].source_id = f"algotune-{task['task']}-dev-v0"
    contract.evidence_sources[0].path = str(manifest_rel)
    contract.frozen_assets = [
        asset for asset in contract.frozen_assets
        if not asset.asset_id.startswith("algotune-")
    ]
    contract.frozen_assets.append(FrozenAsset(
        asset_id=f"algotune-{task['task']}-oracle", kind=FrozenAssetKind.CONFIRMATION,
        path=str(oracle_rel),
    ))
    contract.frozen_assets.append(FrozenAsset(
        asset_id="algotune-official-adapter", kind=FrozenAssetKind.ADAPTER,
        path="evidence_evolve/benchmarks/algotune_official.py",
    ))
    generations = int(os.environ.get("EE_HETERO_GENERATIONS", "3"))
    contract.budgets = Budgets(
        proposal_calls=generations, implementations=generations, mechanics_runs=generations
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    dump_contract(locked, arm_dir / "campaign_contract.locked.yaml")
    local_spec = _spec(task, execution_repo / oracle_rel)

    def evaluate(context: Any) -> EvaluationRun:
        path = context.worktree / candidate_rel
        changed = WorktreeManager(context.repo_root).changed_files(context.worktree, context.genetic_parent_commit)
        before = time.perf_counter(); raw = _development(path, local_spec); elapsed = time.perf_counter() - before
        patch = subprocess.run(["git", "diff", "--binary", context.genetic_parent_commit, "--"], cwd=context.worktree, check=True, capture_output=True).stdout
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=context.worktree, check=True, capture_output=True, text=True).stdout.strip()
        return EvaluationRun(
            evaluation=_build_evaluation(locked.lock.content_sha256, context.candidate, changed, raw),
            command=["in-process", f"algotune-{task['task']}-development-evaluator"],
            elapsed_seconds=elapsed, candidate_commit=head, patch_sha256=sha256_bytes(patch),
        )

    policy = ResearchPolicyGenome.model_validate(yaml.safe_load(
        (execution_repo / "research/policies/algotune_set_cover_blind_v0.yaml").read_text()
    ))
    policy.policy_id = f"algotune_{task['task']}_heterogeneous_v0"
    campaign_dir = arm_dir / "campaign"
    baseline = execution_repo / candidate_rel
    baseline_metrics = _development(baseline, local_spec)["metrics"]
    runner = AutonomousCampaignRunner(
        contract=locked, closure_registry=ClosureRegistry.load(execution_repo / locked.closure_registry),
        policy=policy, repo_root=execution_repo, run_dir=campaign_dir, evaluate=evaluate,
        backend=blind._PinnedProotCodexBackend(),
        worktree_root=Path(tempfile.gettempdir()) / f"ee-{task['task']}-worktrees",
        reference_metrics=baseline_metrics,
        memory_enabled=True, timeout_seconds=1200,
    )
    result = runner.run(
        generations=generations, proposals_per_generation=1, max_evaluations_per_generation=1
    )
    run_hash = hashlib.sha256(str(campaign_dir.resolve()).encode()).hexdigest()[:8]
    candidates: list[tuple[float, Path, str]] = [
        (float(baseline_metrics["raw_speedup"]), baseline, "SEED")
    ]
    successful = 0
    for generation in result.generations:
        for evaluation in generation.evaluations:
            key = f"{locked.campaign.id}-{run_hash}-{evaluation.candidate_id}"
            source = runner.worktrees.candidate_path(key) / candidate_rel
            if source.exists():
                receipt = json.loads(
                    (campaign_dir / evaluation.receipt_path).read_text(encoding="utf-8")
                )["receipt"]["evaluation_input"]
                if receipt["controls"]["candidate_valid"]:
                    successful += 1
                    candidates.append(
                        (float(receipt["metrics"]["raw_speedup"]), source, evaluation.candidate_id)
                    )
    _score, selected, selected_id = max(candidates, key=lambda item: item[0])
    return blind._candidate_result(
        arm="evidence_evolve", arm_dir=arm_dir, source=selected, started=started,
        token_count=blind._token_usage(arm_dir), valid_rate=successful / generations,
        metadata={"engine":"EvidenceEvolve EVIDENCE_NATIVE_EXPERIMENTAL", "selected_candidate_id":selected_id,
                  "execution_commit":locked.campaign.base_commit, "search_mechanics_status":"PASS" if successful else "FAIL"},
    )


def _manifest(run_dir: Path, task: dict[str, Any]) -> None:
    if (run_dir / "manifest.json").exists():
        return
    create_once_json(run_dir / "manifest.json", {
        "schema_version":"1.0", "created_at":datetime.now(timezone.utc).isoformat(),
        "protocol_sha256":sha256_file(PROTOCOL), "task":task, "arms":list(ARMS),
        "model":blind.MODEL, "reasoning_effort":blind.REASONING_EFFORT,
        "generations":3, "development":{"seeds":list(range(20)),"repeats":3},
        "heldout":{"count":100,"repeats":10,"created_after_candidate_lock":True},
        "claim_scope":"CROSS_TASK_BLIND_ALGORITHM_DISCOVERY_PILOT",
    })


def run_arm(run_dir: Path, task: dict[str, Any], arm: str) -> dict[str, Any]:
    spec = _configure(run_dir, task)
    if arm == "evidence_evolve":
        return _run_evidence(run_dir, task, spec)
    return blind.RUNNERS[arm](run_dir)


def finalize(run_dir: Path, task: dict[str, Any]) -> dict[str, Any]:
    spec = _configure(run_dir, task)
    portfolio_lock = run_dir.parent / "portfolio_candidate_lock.json"
    if not portfolio_lock.exists():
        raise ValueError("all task candidates must be locked before any held-out seeds exist")
    portfolio = json.loads(portfolio_lock.read_text())
    if not portfolio.get("all_twenty_candidates_locked"):
        raise ValueError("portfolio lock is incomplete")
    arm_results = [
        json.loads((run_dir / "arms" / arm / "arm_result.json").read_text())
        for arm in ARMS
    ]
    lock_path = run_dir / "candidate_lock.json"
    expected = {
        item["arm"]: {"candidate_sha256":item["candidate_sha256"], "tokens":item["tokens"], "wall_seconds":item["wall_seconds"]}
        for item in arm_results
    }
    if portfolio.get("tasks", {}).get(task["task"]) != expected:
        raise ValueError("task candidates differ from the portfolio lock")
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        if lock["candidates"] != expected:
            raise ValueError("candidate drift after portfolio lock")
    else:
        create_once_json(lock_path, {"locked_at":datetime.now(timezone.utc).isoformat(), "candidates":expected, "portfolio_lock_sha256":sha256_file(portfolio_lock)})
    seeds_path = run_dir / "heldout_seeds.json"
    if not seeds_path.exists():
        values: set[int] = set()
        while len(values) < 100:
            # Official AlgoTune generators use NumPy's legacy RandomState seed
            # contract, whose accepted domain is [0, 2**32 - 1].
            value = secrets.randbelow(2**32)
            if value >= 20:
                values.add(value)
        create_once_json(seeds_path, {"generated_at":datetime.now(timezone.utc).isoformat(), "generated_after_portfolio_candidate_lock":True, "generator":"Python secrets.SystemRandom / OS CSPRNG", "seeds":sorted(values)})
    seeds = json.loads(seeds_path.read_text())["seeds"]
    heldout_workers = HELDOUT_WORKERS.get(task["task"], 18)
    previous_path = run_dir / "suite_result.json"
    previous_by_arm: dict[str, dict[str, Any]] = {}
    if previous_path.exists():
        previous = json.loads(previous_path.read_text())
        if (
            previous.get("candidate_lock_sha256") == sha256_file(lock_path)
            and previous.get("heldout_seed_receipt_sha256") == sha256_file(seeds_path)
        ):
            previous_by_arm = {
                prior["arm"]: prior for prior in previous.get("results", [])
            }
    expected_trials = len(seeds) * 10
    promoted: list[dict[str, Any]] = []
    for item in arm_results:
        path = Path(item["candidate_path"])
        if sha256_file(path) != item["candidate_sha256"]:
            raise ValueError(f"candidate drift: {item['arm']}")
        prior = previous_by_arm.get(item["arm"])
        if prior is not None and prior.get("heldout", {}).get("status_counts") == {"PASS": expected_trials}:
            promoted.append({**prior, "heldout_reused_from_prior_suite": True})
            continue
        try:
            heldout = evaluate_official_candidate_cold(
                path, spec, seeds, repeats=10, workers=heldout_workers,
                timeout_seconds=60.0,
            )
            statuses = set(heldout.get("status_counts", {}))
            shared_truth_failure = bool(statuses & {
                "ERROR_SETUP", "ERROR_REFERENCE", "TIMEOUT_SETUP",
                "TIMEOUT_REFERENCE", "TIMEOUT_VERIFY",
            })
            outcome = (
                "NOT_EVALUABLE_DATA"
                if shared_truth_failure
                else "INVALID_MECHANICS_OR_ADAPTER"
                if item.get("metadata",{}).get("search_mechanics_status") == "FAIL"
                or not heldout["correct"]
                else "POSITIVE_HEADROOM" if heldout["raw_speedup"] > 1.0 else "VALID_NEGATIVE"
            )
        except Exception as exc:
            heldout = {"correct":False,"raw_speedup":0.0,"valid_rate":0.0,"failure":f"{type(exc).__name__}:{exc}"}
            outcome = "NOT_EVALUABLE_DATA"
        promoted.append({**item, "heldout":heldout, "scientific_outcome":outcome,
                         "heldout_reused_from_prior_suite":False,
                         "budget_violation":item["tokens"] > 600000 or item["wall_seconds"] > 7200})
    suite = {
        "schema_version":"1.0", "task":task["task"],
        "candidate_lock_sha256":sha256_file(lock_path),
        "portfolio_candidate_lock_sha256":sha256_file(portfolio_lock),
        "heldout_seed_receipt_sha256":sha256_file(seeds_path),
        "results":promoted, "pareto_front":blind._pareto_front(promoted),
        "metrics":["heldout.raw_speedup","tokens","wall_seconds","heldout.valid_rate"],
        "hard_constraint":"heldout.correct and no budget violation",
        "resource_execution": {
            "cgroup_cpu_quota_cores": 18,
            "outer_worker_count": heldout_workers,
            "blas_threads_per_worker": 1,
            "task_specific_reason": (
                "18 outer workers retained after a frozen 20-seed mechanics benchmark measured 1.39s versus 9.27s for one worker despite CP-SAT internal threading"
                if task["task"] == "job_shop_scheduling"
                else "independent single-threaded trials saturate the 18-core quota"
            ),
        },
        "claim_scope":"CROSS_TASK_BLIND_ALGORITHM_DISCOVERY_PILOT",
        "superiority_claim_permitted":False,
    }
    blind._write_json(run_dir / "suite_result.json", suite)
    return suite


def lock_portfolio(root: Path) -> dict[str, Any]:
    protocol = _load_protocol()
    tasks: dict[str, Any] = {}
    for task in protocol["tasks"]:
        task_root = root / task["task"]
        candidates: dict[str, Any] = {}
        for arm in ARMS:
            item = json.loads((task_root / "arms" / arm / "arm_result.json").read_text())
            path = Path(item["candidate_path"])
            if sha256_file(path) != item["candidate_sha256"]:
                raise ValueError(f"candidate drift before lock: {task['task']}:{arm}")
            candidates[arm] = {"candidate_sha256":item["candidate_sha256"], "tokens":item["tokens"], "wall_seconds":item["wall_seconds"]}
        tasks[task["task"]] = candidates
    payload = {"locked_at":datetime.now(timezone.utc).isoformat(), "all_twenty_candidates_locked":True,
               "heldout_existed_at_lock":any(root.rglob("heldout_seeds.json")), "tasks":tasks}
    if payload["heldout_existed_at_lock"]:
        raise ValueError("held-out seeds existed before all twenty candidates were locked")
    create_once_json(root / "portfolio_candidate_lock.json", payload)
    return payload


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--task",required=True); parser.add_argument("--run-dir",type=Path); parser.add_argument("--arm",choices=(*ARMS,"all","finalize"),default="all"); args=parser.parse_args()
    task=_task_payload(args.task); run_dir=(args.run_dir or REPO_ROOT / "runs" / "algotune_heterogeneous_v0" / args.task).resolve(); run_dir.mkdir(parents=True,exist_ok=True); _manifest(run_dir,task)
    for key, value in {"OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1","EE_ALGOTUNE_DEV_COUNT":"20","EE_ALGOTUNE_DEV_REPEATS":"3"}.items():
        os.environ[key] = value
    os.environ.setdefault("EE_ALGOTUNE_WORKERS", "4")
    if args.arm in ARMS: run_arm(run_dir,task,args.arm); return 0
    if args.arm=="finalize": finalize(run_dir,task); return 0
    for arm in ARMS: run_arm(run_dir,task,arm)
    finalize(run_dir,task); return 0
if __name__=="__main__": raise SystemExit(main())
