from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evidence_evolve.artifacts import create_once_json
from evidence_evolve.backends.codex_cli import CodexCliBackend, CodexRole
if os.name == "nt":
    ProotCodexCliBackend = CodexCliBackend
else:
    from evidence_evolve.backends.proot_codex import ProotCodexCliBackend
from evidence_evolve.discovery.autonomous import AutonomousCampaignRunner
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import ProtocolLock, dump_contract, load_contract
from evidence_evolve.hashing import sha256_file, sha256_object
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_evolve.models import Budgets
from evidence_evolve.proposals.models import ProposalMaterializerMode
from evidence_evolve.search.models import SearchRunRequest
from evidence_evolve.search.shinka_native import ShinkaNativeEngine
from tasks.algotune_set_cover.autonomous_adapter import evaluate_candidate as evaluate_ee
from tasks.algotune_set_cover.campaign_evaluator import evaluate_development
from tasks.algotune_set_cover.common import evaluate_candidate


REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = REPO_ROOT / "tasks" / "algotune_set_cover"
INITIAL = TASK_ROOT / "initial.py"
TASK_PROMPT = (
    "Optimize the exact set-cover solver below for runtime on deterministic "
    "development inputs. Preserve class Solver and solve(problem), return an exact "
    "minimum-cardinality 1-based cover for every input."
)
CONTRACT_TEMPLATE = REPO_ROOT / "research" / "contracts" / "algotune_set_cover_blind_v0.template.yaml"
POLICY_PATH = REPO_ROOT / "research" / "policies" / "algotune_set_cover_blind_v0.yaml"
CANDIDATE_RELATIVE = Path("tasks/algotune_set_cover/initial.py")
CAMPAIGN_ID = "algotune-set-cover-ee-experimental"
ARMS = ("shinka", "ada", "vanilla", "evidence_evolve", "evox")
MODEL = "gpt-5.6-terra"
REASONING_EFFORT = "high"
GENERATIONS = 3
DEV_COUNT = 100
DEV_REPEATS = 3
TEST_COUNT = 100
TEST_REPEATS = 10
TOKEN_CEILING = 600_000
WALL_CEILING_SECONDS = 7_200
EVALUATOR_WORKERS = 9
_SKY_CLIENT_IDS = itertools.count(1)


class _PinnedCodexBackend(CodexCliBackend):
    """Pilot-local model pin without changing the frozen shared backend."""

    def __init__(self) -> None:
        super().__init__(os.environ.get("EVIDENCE_EVOLVE_CODEX_EXECUTABLE", "codex"))

    def build_command(self, **kwargs: Any) -> list[str]:
        command = super().build_command(**kwargs)
        command[-1:-1] = ["--model", MODEL]
        return command


class _PinnedProotCodexBackend(ProotCodexCliBackend):
    """Pilot-local model pin plus isolated writable implementer bridge."""

    def __init__(self) -> None:
        super().__init__(os.environ.get("EVIDENCE_EVOLVE_CODEX_EXECUTABLE", "codex"))

    def build_command(self, **kwargs: Any) -> list[str]:
        command = super().build_command(**kwargs)
        command[-1:-1] = ["--model", MODEL]
        return command


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


def _token_usage(root: Path) -> int:
    total = 0
    for path in root.rglob("*.events.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict):
                total += int(usage.get("input_tokens", 0) or 0)
                total += int(usage.get("output_tokens", 0) or 0)
    return total


def _headless_token_usage(path: Path) -> int:
    total = 0
    if not path.is_file():
        return total
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            continue
        total += int(usage.get("inputTokens", 0) or 0)
        total += int(usage.get("cacheReadTokens", 0) or 0)
        total += int(usage.get("outputTokens", 0) or 0)
    return total


def _resource_receipt() -> dict[str, Any]:
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "cpu_affinity_count": len(os.sched_getaffinity(0)),
        "cgroup_cpu_max": cpu_max.read_text(encoding="utf-8").strip()
        if cpu_max.is_file()
        else None,
        "system_memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
        "visible_gpus": gpu.stdout.strip().splitlines() if gpu.returncode == 0 else [],
        "worker_policy": "one isolated arm at a time; nine fixed independent seed workers",
    }


def _manifest(run_dir: Path) -> None:
    if (run_dir / "manifest.json").is_file():
        return
    create_once_json(
        run_dir / "manifest.json",
        {
            "schema_version": "1.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "repo_commit": _git(REPO_ROOT, "rev-parse", "HEAD"),
            "source_snapshot_sha256": sha256_object(
                {
                    str(path.relative_to(REPO_ROOT)): sha256_file(path)
                    for path in sorted(
                        [
                            *TASK_ROOT.glob("*"),
                            Path(__file__).resolve(),
                            CONTRACT_TEMPLATE,
                            POLICY_PATH,
                        ]
                    )
                    if path.is_file()
                }
            ),
            "arms": list(ARMS),
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "generations": GENERATIONS,
            "proposal_calls_per_arm": GENERATIONS,
            "native_generation_mapping": {
                "shinka": GENERATIONS + 1,
                "others": GENERATIONS,
            },
            "development": {
                "seeds": list(range(DEV_COUNT)),
                "repeats": DEV_REPEATS,
            },
            "heldout": {
                "instance_count": TEST_COUNT,
                "repeats": TEST_REPEATS,
                "generation": "CSPRNG seeds generated only after candidate_lock.json exists",
            },
            "limits_per_arm": {
                "observed_token_ceiling": TOKEN_CEILING,
                "wall_seconds": WALL_CEILING_SECONDS,
            },
            "resources": _resource_receipt(),
            "evaluator_workers": EVALUATOR_WORKERS,
            "claim_scope": "ALGORITHM_DISCOVERY_BLIND_PILOT_ONLY",
        },
    )


def _candidate_result(
    *,
    arm: str,
    arm_dir: Path,
    source: Path,
    started: float,
    token_count: int,
    valid_rate: float,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    selected = arm_dir / "selected_candidate.py"
    if source.resolve() != selected.resolve():
        shutil.copy2(source, selected)
    dev = evaluate_development(selected)
    result = {
        "arm": arm,
        "candidate_path": str(selected.resolve()),
        "candidate_sha256": sha256_file(selected),
        "development": dev,
        "tokens": token_count,
        "wall_seconds": time.perf_counter() - started,
        "proposal_valid_rate": valid_rate,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "metadata": metadata,
    }
    _write_json(arm_dir / "arm_result.json", result)
    return result


def _ensure_evox_amendment(run_dir: Path) -> None:
    candidate_lock = run_dir / "candidate_lock.json"
    if candidate_lock.exists():
        raise ValueError("EvoX cannot be added after candidate promotion lock")
    amendment = run_dir / "protocol_amendment_evox.json"
    if amendment.exists():
        return
    create_once_json(
        amendment,
        {
            "schema_version": "1.0",
            "amended_at": datetime.now(timezone.utc).isoformat(),
            "authority": "USER_REQUESTED_BEFORE_CANDIDATE_LOCK",
            "change": "add SkyDiscover EvoX as a fifth independent arm",
            "unchanged_conditions": {
                "model": MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "proposal_budget": GENERATIONS,
                "development_seeds": list(range(DEV_COUNT)),
                "development_repeats": DEV_REPEATS,
                "token_ceiling": TOKEN_CEILING,
                "wall_ceiling_seconds": WALL_CEILING_SECONDS,
                "evaluator_workers": EVALUATOR_WORKERS,
                "single_final_candidate": True,
            },
            "original_manifest_sha256": sha256_file(run_dir / "manifest.json"),
            "evox_config_sha256": sha256_file(TASK_ROOT / "evox.yaml"),
            "runner_sha256": sha256_file(Path(__file__).resolve()),
            "heldout_existed_at_amendment": False,
        },
    )


def _run_codex(
    *,
    backend: CodexCliBackend,
    prompt: str,
    workdir: Path,
    output_dir: Path,
    call_id: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    schema_path = output_dir / f"{call_id}.schema.json"
    output_path = output_dir / f"{call_id}.json"
    _write_json(schema_path, schema)
    result = backend.run(
        role=CodexRole("hypothesis_explorer", writable=False),
        prompt=prompt,
        workdir=workdir,
        output_schema=schema_path,
        output_path=output_path,
        events_path=output_dir / f"{call_id}.events.jsonl",
        stderr_path=output_dir / f"{call_id}.stderr.log",
        timeout_seconds=1_200,
    )
    if result.get("status") != "PASS" or not output_path.is_file():
        raise RuntimeError(f"Codex call {call_id} failed: {result}")
    return json.loads(output_path.read_text(encoding="utf-8"))


def run_vanilla(run_dir: Path) -> dict[str, Any]:
    arm = "vanilla"
    arm_dir = run_dir / "arms" / arm
    result_path = arm_dir / "arm_result.json"
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    started = time.perf_counter()
    backend = _PinnedCodexBackend()
    incumbent = INITIAL.read_text(encoding="utf-8")
    incumbent_path = arm_dir / "candidates" / "seed.py"
    incumbent_path.parent.mkdir(parents=True, exist_ok=True)
    incumbent_path.write_text(incumbent, encoding="utf-8")
    incumbent_metrics = evaluate_development(incumbent_path)
    valid = 0
    history: list[dict[str, Any]] = []
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["code", "rationale"],
        "additionalProperties": False,
    }
    for index in range(1, GENERATIONS + 1):
        prompt = (
            f"{TASK_PROMPT} Do not access benchmark, "
            "evaluator, run, or test files. Return the complete Python file.\n\n"
            f"Current development result:\n{json.dumps(incumbent_metrics)}\n\n"
            f"Current code:\n```python\n{incumbent}\n```"
        )
        payload = _run_codex(
            backend=backend,
            prompt=prompt,
            workdir=TASK_ROOT,
            output_dir=arm_dir / "calls",
            call_id=f"iteration_{index:03d}",
            schema=schema,
        )
        candidate_path = arm_dir / "candidates" / f"candidate_{index:03d}.py"
        candidate_path.write_text(str(payload["code"]), encoding="utf-8")
        metrics = evaluate_development(candidate_path)
        is_valid = bool(metrics["controls"].get("candidate_valid"))
        valid += int(is_valid)
        history.append({"iteration": index, "metrics": metrics, "rationale": payload["rationale"]})
        if is_valid and float(metrics["metrics"]["raw_speedup"]) > float(
            incumbent_metrics["metrics"]["raw_speedup"]
        ):
            incumbent = candidate_path.read_text(encoding="utf-8")
            incumbent_path = candidate_path
            incumbent_metrics = metrics
        if _token_usage(arm_dir) >= TOKEN_CEILING:
            break
    _write_json(arm_dir / "history.json", history)
    return _candidate_result(
        arm=arm,
        arm_dir=arm_dir,
        source=incumbent_path,
        started=started,
        token_count=_token_usage(arm_dir),
        valid_rate=valid / GENERATIONS,
        metadata={"control": "independent iterative Codex calls; no population or archive"},
    )


class _SkyCodexClient:
    def __init__(self, _config: Any, *, arm_dir: Path):
        self.arm_dir = arm_dir
        self.backend = _PinnedCodexBackend()
        self.client_id = f"client_{next(_SKY_CLIENT_IDS):02d}"
        self.call_index = 0

    async def generate(
        self, system_message: str, messages: list[dict[str, Any]], **_kwargs: Any
    ) -> Any:
        from skydiscover.llm.base import LLMResponse

        self.call_index += 1
        rendered = "\n\n".join(
            f"{item.get('role', 'user').upper()}: {item.get('content', '')}"
            for item in messages
        )
        payload = await asyncio.to_thread(
            _run_codex,
            backend=self.backend,
            prompt=f"{system_message}\n\n{rendered}",
            workdir=TASK_ROOT,
            output_dir=self.arm_dir / "calls",
            call_id=f"{self.client_id}_call_{self.call_index:03d}",
            schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        )
        return LLMResponse(text=str(payload["text"]))


def run_ada(run_dir: Path) -> dict[str, Any]:
    arm = "ada"
    arm_dir = run_dir / "arms" / arm
    result_path = arm_dir / "arm_result.json"
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    started = time.perf_counter()
    from skydiscover import run_discovery
    from skydiscover.config import LLMModelConfig, load_config

    config = load_config(TASK_ROOT / "adaevolve.yaml")
    model_config = LLMModelConfig(
        name=MODEL,
        temperature=0.0,
        max_tokens=32768,
        timeout=1200,
        retries=0,
        reasoning_effort=REASONING_EFFORT,
        init_client=lambda cfg: _SkyCodexClient(cfg, arm_dir=arm_dir),
    )
    config.llm.models = [model_config]
    config.llm.evaluator_models = [model_config]
    config.llm.guide_models = [model_config]
    output_dir = arm_dir / "upstream"
    result = run_discovery(
        evaluator=TASK_ROOT / "evaluator.py",
        initial_program=INITIAL,
        iterations=GENERATIONS,
        config=config,
        output_dir=str(output_dir),
        cleanup=False,
    )
    selected = arm_dir / "ada_best.py"
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(result.best_solution or INITIAL.read_text(encoding="utf-8"), encoding="utf-8")
    valid = 0
    attempted = 0
    for info in output_dir.rglob("best_program_info.json"):
        attempted += 1
        payload = json.loads(info.read_text(encoding="utf-8"))
        valid += int(bool(payload.get("metrics", {}).get("correct")))
    return _candidate_result(
        arm=arm,
        arm_dir=arm_dir,
        source=selected,
        started=started,
        token_count=_token_usage(arm_dir),
        valid_rate=(valid / attempted if attempted else 0.0),
        metadata={
            "engine": "SkyDiscover AdaEvolve",
            "upstream_output": str(output_dir.resolve()),
            "upstream_best_score": result.best_score,
            "sky_commit": "8a840394e19ee4bfb3fb0a62762b902561a7efeb",
        },
    )


def run_evox(run_dir: Path) -> dict[str, Any]:
    arm = "evox"
    arm_dir = run_dir / "arms" / arm
    result_path = arm_dir / "arm_result.json"
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    _ensure_evox_amendment(run_dir)
    repair_path = run_dir / "adapter_repair_evox.json"
    if not repair_path.exists():
        amendment = json.loads(
            (run_dir / "protocol_amendment_evox.json").read_text(encoding="utf-8")
        )
        create_once_json(
            repair_path,
            {
                "schema_version": "1.0",
                "authority": "MECHANICS_REPAIR_BEFORE_CANDIDATE_LOCK",
                "issue": "solution and meta-search clients collided on log filenames",
                "repair": "assign a process-unique prefix to every SkyDiscover Codex client",
                "failed_admission_artifacts": "admission_failures/evox_client_log_collision",
                "previous_runner_sha256": amendment["runner_sha256"],
                "repaired_runner_sha256": sha256_file(Path(__file__).resolve()),
                "heldout_existed_at_repair": False,
            },
        )
    started = time.perf_counter()
    from skydiscover import run_discovery
    from skydiscover.config import LLMModelConfig, load_config

    config = load_config(TASK_ROOT / "evox.yaml")
    model_config = LLMModelConfig(
        name=MODEL,
        temperature=0.0,
        max_tokens=32768,
        timeout=1200,
        retries=0,
        reasoning_effort=REASONING_EFFORT,
        init_client=lambda cfg: _SkyCodexClient(cfg, arm_dir=arm_dir),
    )
    config.llm.models = [model_config]
    config.llm.evaluator_models = [model_config]
    config.llm.guide_models = [model_config]
    output_dir = arm_dir / "upstream"
    result = run_discovery(
        evaluator=TASK_ROOT / "evaluator.py",
        initial_program=INITIAL,
        iterations=GENERATIONS,
        config=config,
        output_dir=str(output_dir),
        cleanup=False,
    )
    selected = arm_dir / "evox_best.py"
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(
        result.best_solution or INITIAL.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    programs = list(output_dir.glob("checkpoints/checkpoint_*/programs/*.json"))
    by_id: dict[str, dict[str, Any]] = {}
    for path in programs:
        payload = json.loads(path.read_text(encoding="utf-8"))
        by_id[str(payload.get("id"))] = payload
    proposals = [
        item for item in by_id.values() if int(item.get("iteration_found", 0)) > 0
    ]
    valid = sum(int(bool(item.get("metrics", {}).get("correct"))) for item in proposals)
    return _candidate_result(
        arm=arm,
        arm_dir=arm_dir,
        source=selected,
        started=started,
        token_count=_token_usage(arm_dir),
        valid_rate=(valid / len(proposals) if proposals else 0.0),
        metadata={
            "engine": "SkyDiscover EvoX",
            "upstream_output": str(output_dir.resolve()),
            "upstream_best_score": result.best_score,
            "sky_commit": "8a840394e19ee4bfb3fb0a62762b902561a7efeb",
            "generation_budget": GENERATIONS,
            "token_accounting": "Codex input + output tokens; cached input is included in input",
        },
    )


def run_shinka(run_dir: Path) -> dict[str, Any]:
    arm = "shinka"
    arm_dir = run_dir / "arms" / arm
    result_path = arm_dir / "arm_result.json"
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    started = time.perf_counter()
    task_dir = arm_dir / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(INITIAL, task_dir / "initial.py")
    shutil.copy2(TASK_ROOT / "evaluator.py", task_dir / "evaluate.py")
    shutil.copy2(TASK_ROOT / "shinka.yaml", task_dir / "shinka.yaml")
    results_dir = arm_dir / "upstream"
    prior = os.environ.get("SHINKA_HEADLESS_COMMAND")
    prior_usage_log = os.environ.get("EE_HEADLESS_USAGE_LOG")
    usage_log = arm_dir / "headless_usage.jsonl"
    os.environ["SHINKA_HEADLESS_COMMAND"] = (
        f'"{sys.executable}" "{TASK_ROOT / "codex_headless.py"}"'
    )
    os.environ["EE_HEADLESS_USAGE_LOG"] = str(usage_log)
    try:
        receipt = ShinkaNativeEngine().run(
            SearchRunRequest(
                run_id="algotune-blind-shinka",
                task_dir=task_dir,
                results_dir=results_dir,
                num_generations=GENERATIONS + 1,
                config_fname="shinka.yaml",
                max_evaluation_jobs=1,
                max_proposal_jobs=1,
                max_db_workers=1,
                verbose=False,
                proposal_materializer=ProposalMaterializerMode.EVIDENCE_EVOLVE_V1,
            )
        )
    finally:
        if prior is None:
            os.environ.pop("SHINKA_HEADLESS_COMMAND", None)
        else:
            os.environ["SHINKA_HEADLESS_COMMAND"] = prior
        if prior_usage_log is None:
            os.environ.pop("EE_HEADLESS_USAGE_LOG", None)
        else:
            os.environ["EE_HEADLESS_USAGE_LOG"] = prior_usage_log
    connection = sqlite3.connect(results_dir / "programs.sqlite")
    try:
        row = connection.execute(
            "SELECT code FROM programs WHERE id = ?",
            (receipt.imported.best_program_id,),
        ).fetchone()
        proposal_counts = connection.execute(
            "SELECT COUNT(*), SUM(CASE WHEN correct THEN 1 ELSE 0 END) "
            "FROM programs WHERE generation > 0"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("Shinka best program is absent from its native database")
    selected = arm_dir / "shinka_best.py"
    selected.write_text(str(row[0]), encoding="utf-8")
    return _candidate_result(
        arm=arm,
        arm_dir=arm_dir,
        source=selected,
        started=started,
        token_count=_headless_token_usage(usage_log),
        valid_rate=(float(proposal_counts[1] or 0) / float(proposal_counts[0] or 1)),
        metadata={
            "engine": "official ShinkaEvolve 0.0.7",
            "source_commit": receipt.upstream_source_commit,
            "proposal_materializer": receipt.proposal_materializer.value,
            "claim_scope": receipt.claim_scope,
            "token_accounting": "headless input + cache-read + output tokens",
        },
    )


def _source_snapshot(repo_root: Path) -> tuple[list[str], str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    paths = sorted(
        path
        for path in completed.stdout.split("\0")
        if path
        and not path.startswith("runs/")
        and not path.startswith(".evolve-worktrees/")
    )
    hashes = {path: sha256_file(repo_root / path) for path in paths}
    return paths, sha256_object(hashes)


def _prepare_execution_repo(run_dir: Path) -> Path:
    execution_repo = run_dir / "execution_repo"
    manifest_path = run_dir / "execution_repo_snapshot.json"
    paths, snapshot_sha256 = _source_snapshot(REPO_ROOT)
    if execution_repo.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_snapshot_sha256"] != snapshot_sha256:
            raise ValueError("source worktree drifted after execution snapshot creation")
        return execution_repo
    execution_repo.mkdir(parents=True)
    for relative in paths:
        source = REPO_ROOT / relative
        destination = execution_repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    _git(execution_repo, "init", "-b", "master")
    _git(execution_repo, "add", ".")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=EvidenceEvolve Blind Pilot",
            "-c",
            "user.email=blind-pilot@invalid.local",
            "commit",
            "-m",
            "Frozen AlgoTune blind-pilot execution snapshot",
        ],
        cwd=execution_repo,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    create_once_json(
        manifest_path,
        {
            "source_snapshot_sha256": snapshot_sha256,
            "execution_commit": _git(execution_repo, "rev-parse", "HEAD"),
            "file_count": len(paths),
        },
    )
    return execution_repo


def _runtime_contract(execution_repo: Path, arm_dir: Path) -> Any:
    template_relative = CONTRACT_TEMPLATE.resolve().relative_to(REPO_ROOT)
    contract = load_contract(execution_repo / template_relative)
    contract.campaign.id = CAMPAIGN_ID
    contract.campaign.base_commit = _git(execution_repo, "rev-parse", "HEAD")
    contract.budgets = Budgets(
        proposal_calls=GENERATIONS,
        implementations=GENERATIONS,
        mechanics_runs=GENERATIONS,
    )
    contract.lock = None
    locked = ProtocolLock(execution_repo).lock(contract)
    ProtocolLock(execution_repo).assert_valid(locked)
    dump_contract(locked, arm_dir / "campaign_contract.locked.yaml")
    return locked


def run_evidence_evolve(run_dir: Path) -> dict[str, Any]:
    arm = "evidence_evolve"
    arm_dir = run_dir / "arms" / arm
    result_path = arm_dir / "arm_result.json"
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    started = time.perf_counter()
    execution_repo = _prepare_execution_repo(run_dir)
    contract = _runtime_contract(execution_repo, arm_dir)
    policy_payload = yaml.safe_load(
        (
            execution_repo
            / POLICY_PATH.resolve().relative_to(REPO_ROOT)
        ).read_text(encoding="utf-8")
    )
    policy = ResearchPolicyGenome.model_validate(policy_payload)
    campaign_dir = arm_dir / "campaign"
    worktree_root = Path(tempfile.gettempdir()) / "ee-algotune-blind-worktrees"
    baseline = execution_repo / CANDIDATE_RELATIVE
    baseline_metrics = evaluate_development(baseline)["metrics"]
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(
            execution_repo / contract.closure_registry
        ),
        policy=policy,
        repo_root=execution_repo,
        run_dir=campaign_dir,
        evaluate=evaluate_ee,
        backend=_PinnedProotCodexBackend(),
        worktree_root=worktree_root,
        reference_metrics=baseline_metrics,
        memory_enabled=True,
        timeout_seconds=1_200,
    )
    result = runner.run(
        generations=GENERATIONS,
        proposals_per_generation=1,
        max_evaluations_per_generation=1,
    )
    run_hash = hashlib.sha256(str(campaign_dir.resolve()).encode("utf-8")).hexdigest()[:8]
    candidates: list[tuple[float, Path, str]] = [
        (
            float(baseline_metrics["raw_speedup"]),
            baseline,
            "SEED",
        )
    ]
    successful = 0
    for generation in result.generations:
        for evaluation in generation.evaluations:
            candidate_id = evaluation.candidate_id
            key = f"{contract.campaign.id}-{run_hash}-{candidate_id}"
            source = runner.worktrees.candidate_path(key) / CANDIDATE_RELATIVE
            if not source.is_file():
                continue
            receipt = json.loads(
                (campaign_dir / evaluation.receipt_path).read_text(encoding="utf-8")
            )["receipt"]["evaluation_input"]
            if bool(receipt["controls"].get("candidate_valid")):
                successful += 1
                candidates.append(
                    (float(receipt["metrics"]["raw_speedup"]), source, candidate_id)
                )
    _score, selected, selected_id = max(candidates, key=lambda item: item[0])
    return _candidate_result(
        arm=arm,
        arm_dir=arm_dir,
        source=selected,
        started=started,
        token_count=_token_usage(arm_dir),
        valid_rate=successful / GENERATIONS,
        metadata={
            "engine": "EvidenceEvolve EVIDENCE_NATIVE_EXPERIMENTAL",
            "selected_candidate_id": selected_id,
            "execution_commit": contract.campaign.base_commit,
            "scientific_memory_enabled": True,
            "implementer_sandbox_bridge": "proot-nobody",
            "search_mechanics_status": "PASS" if successful else "FAIL",
            "baseline_fallback": selected_id == "SEED",
        },
    )


def _pareto_front(results: list[dict[str, Any]]) -> list[str]:
    eligible = [
        item
        for item in results
        if item["heldout"]["correct"]
        and not item["budget_violation"]
        and item["scientific_outcome"]
        not in {"INVALID_MECHANICS_OR_ADAPTER", "NOT_EVALUABLE_DATA"}
    ]
    front: list[str] = []
    for candidate in eligible:
        dominated = False
        for challenger in eligible:
            if challenger is candidate:
                continue
            no_worse = (
                challenger["heldout"]["raw_speedup"]
                >= candidate["heldout"]["raw_speedup"]
                and challenger["tokens"] <= candidate["tokens"]
                and challenger["wall_seconds"] <= candidate["wall_seconds"]
            )
            strict = (
                challenger["heldout"]["raw_speedup"]
                > candidate["heldout"]["raw_speedup"]
                or challenger["tokens"] < candidate["tokens"]
                or challenger["wall_seconds"] < candidate["wall_seconds"]
            )
            if no_worse and strict:
                dominated = True
                break
        if not dominated:
            front.append(str(candidate["arm"]))
    return sorted(front)


def finalize(run_dir: Path) -> dict[str, Any]:
    arm_results: list[dict[str, Any]] = []
    for arm in ARMS:
        path = run_dir / "arms" / arm / "arm_result.json"
        if not path.is_file():
            raise FileNotFoundError(f"arm has no locked final candidate: {arm}")
        arm_results.append(json.loads(path.read_text(encoding="utf-8")))
    candidate_lock = {
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "candidates": {
            item["arm"]: {
                "candidate_sha256": item["candidate_sha256"],
                "tokens": item["tokens"],
                "wall_seconds": item["wall_seconds"],
            }
            for item in arm_results
        },
        "heldout_instances_existed_at_lock": False,
    }
    candidate_lock_path = run_dir / "candidate_lock.json"
    if candidate_lock_path.is_file():
        existing_lock = json.loads(candidate_lock_path.read_text(encoding="utf-8"))
        if existing_lock.get("candidates") != candidate_lock["candidates"]:
            raise ValueError("final candidates drifted after candidate lock")
        candidate_lock = existing_lock
    else:
        create_once_json(candidate_lock_path, candidate_lock)
    seeds_path = run_dir / "heldout_seeds.json"
    if not seeds_path.is_file():
        seeds: set[int] = set()
        while len(seeds) < TEST_COUNT:
            seed = secrets.randbits(63)
            if seed >= DEV_COUNT:
                seeds.add(seed)
        create_once_json(
            seeds_path,
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "generated_after_candidate_lock": True,
                "generator": "Python secrets.SystemRandom / OS CSPRNG",
                "seeds": sorted(seeds),
            },
        )
    heldout_seeds = json.loads(seeds_path.read_text(encoding="utf-8"))["seeds"]
    promoted: list[dict[str, Any]] = []
    for item in arm_results:
        candidate_path = Path(item["candidate_path"])
        if sha256_file(candidate_path) != item["candidate_sha256"]:
            raise ValueError(f"candidate changed after selection: {item['arm']}")
        try:
            heldout = evaluate_candidate(
                candidate_path,
                heldout_seeds,
                repeats=TEST_REPEATS,
            )
            outcome = (
                "INVALID_MECHANICS_OR_ADAPTER"
                if item.get("metadata", {}).get("search_mechanics_status") == "FAIL"
                or not heldout["correct"]
                else "POSITIVE_HEADROOM"
                if float(heldout["raw_speedup"]) > 1.0
                else "VALID_NEGATIVE"
            )
        except Exception as exc:
            heldout = {
                "correct": False,
                "raw_speedup": 0.0,
                "valid_rate": 0.0,
                "failure": f"{type(exc).__name__}:{exc}",
            }
            outcome = "NOT_EVALUABLE_DATA"
        promoted.append(
            {
                **item,
                "heldout": heldout,
                "scientific_outcome": outcome,
                "budget_violation": bool(
                    item["tokens"] > TOKEN_CEILING
                    or item["wall_seconds"] > WALL_CEILING_SECONDS
                ),
            }
        )
    suite = {
        "schema_version": "1.0",
        "candidate_lock_sha256": sha256_file(candidate_lock_path),
        "heldout_seed_receipt_sha256": sha256_file(seeds_path),
        "protocol_amendment_evox_sha256": sha256_file(
            run_dir / "protocol_amendment_evox.json"
        ),
        "adapter_repair_evox_sha256": sha256_file(
            run_dir / "adapter_repair_evox.json"
        ),
        "results": promoted,
        "pareto_front": _pareto_front(promoted),
        "metrics": ["heldout.raw_speedup", "tokens", "wall_seconds", "heldout.valid_rate"],
        "hard_constraint": "heldout.correct and no budget violation",
        "claim_scope": "ALGORITHM_DISCOVERY_BLIND_PILOT_ONLY",
        "superiority_claim_permitted": False,
    }
    _write_json(run_dir / "suite_result.json", suite)
    return suite


RUNNERS = {
    "shinka": run_shinka,
    "ada": run_ada,
    "vanilla": run_vanilla,
    "evidence_evolve": run_evidence_evolve,
    "evox": run_evox,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AlgoTune set-cover blind pilot")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=REPO_ROOT / "runs" / "algotune_set_cover_blind_v0",
    )
    parser.add_argument("--arm", choices=(*ARMS, "all", "finalize"), default="all")
    args = parser.parse_args()
    os.environ.setdefault("EE_ALGOTUNE_WORKERS", str(EVALUATOR_WORKERS))
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    _manifest(run_dir)
    if args.arm in RUNNERS:
        RUNNERS[args.arm](run_dir)
        return 0
    if args.arm == "finalize":
        finalize(run_dir)
        return 0
    for arm in ARMS:
        subprocess.run(
            [
                str(REPO_ROOT / ".venv" / "bin" / "python"),
                "-m",
                "evidence_evolve.benchmarks.algotune_blind",
                "--run-dir",
                str(run_dir),
                "--arm",
                arm,
            ],
            cwd=REPO_ROOT,
            check=True,
            timeout=WALL_CEILING_SECONDS,
            env={
                **os.environ,
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "EE_ALGOTUNE_DEV_COUNT": str(DEV_COUNT),
                "EE_ALGOTUNE_DEV_REPEATS": str(DEV_REPEATS),
                "EE_ALGOTUNE_WORKERS": str(EVALUATOR_WORKERS),
            },
        )
    finalize(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
