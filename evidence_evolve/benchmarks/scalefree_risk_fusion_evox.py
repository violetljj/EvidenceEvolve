from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import os
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_evolve.backends.codex_cli import CodexCliBackend, CodexRole


REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = REPO_ROOT / "tasks" / "scalefree_risk_fusion_v0"
INITIAL = TASK_ROOT / "candidate.py"
EVALUATOR = TASK_ROOT / "evaluator.py"
CONFIG = TASK_ROOT / "evox.yaml"
MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "high"
EXPECTED_SKY_COMMIT = "8a840394e19ee4bfb3fb0a62762b902561a7efeb"
DEFAULT_SKY_ROOT = Path("/root/autodl-tmp/external-benchmarks/skydiscover")
_CLIENT_IDS = itertools.count(1)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _resource_receipt() -> dict[str, object]:
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free,utilization.gpu",
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
        "execution_policy": "EvoX solution iterations are inherently sequential; per-candidate vector metrics use the allocated CPU without nested evaluator processes",
    }


class _SolCodexBackend(CodexCliBackend):
    def build_command(self, **kwargs: Any) -> list[str]:
        command = super().build_command(**kwargs)
        command[-1:-1] = ["--model", MODEL, "-c", f'model_reasoning_effort="{REASONING_EFFORT}"']
        return command


def _run_codex(
    *, backend: CodexCliBackend, prompt: str, workdir: Path, call_dir: Path, call_id: str
) -> dict[str, Any]:
    call_dir.mkdir(parents=True, exist_ok=True)
    schema_path = call_dir / f"{call_id}.schema.json"
    output_path = call_dir / f"{call_id}.json"
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    _write_json(schema_path, schema)
    result = backend.run(
        role=CodexRole("hypothesis_explorer", writable=False),
        prompt=prompt,
        workdir=workdir,
        output_schema=schema_path,
        output_path=output_path,
        events_path=call_dir / f"{call_id}.events.jsonl",
        stderr_path=call_dir / f"{call_id}.stderr.log",
        timeout_seconds=1800,
    )
    receipt = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "call_id": call_id,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "backend_result": result,
    }
    _write_json(call_dir / f"{call_id}.receipt.json", receipt)
    if result.get("status") != "PASS" or not output_path.is_file():
        raise RuntimeError(f"Codex call failed: {call_id}: {result}")
    return json.loads(output_path.read_text(encoding="utf-8"))


class _SkyCodexClient:
    def __init__(self, _config: Any, *, run_dir: Path):
        self.run_dir = run_dir
        self.backend = _SolCodexBackend()
        self.client_id = f"client_{next(_CLIENT_IDS):02d}"
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
            call_dir=self.run_dir / "calls",
            call_id=f"{self.client_id}_call_{self.call_index:04d}",
        )
        return LLMResponse(text=str(payload["text"]))


def _token_usage(root: Path) -> dict[str, int]:
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for path in root.glob("calls/*.events.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = event.get("usage") if isinstance(event, dict) else None
            if not isinstance(usage, dict):
                continue
            totals["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            totals["cached_input_tokens"] += int(usage.get("cached_input_tokens", 0) or 0)
            totals["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
    totals["total_tokens_including_cached"] = sum(totals.values())
    return totals


def _build_iteration_summary(run_dir: Path) -> list[dict[str, Any]]:
    ledger = run_dir / "evaluations.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    summaries = []
    best = float("-inf")
    for record in records:
        score = float(record["metrics"].get("combined_score", 0.0))
        best = max(best, score)
        summaries.append(
            {
                "evaluation_index": record["evaluation_index"],
                "evox_iteration": record["evox_iteration"],
                "candidate_sha256": record["candidate_sha256"],
                "combined_score": score,
                "best_score_so_far": best,
                "valid": bool(record["metrics"].get("valid")),
                "metrics": record["metrics"],
            }
        )
    _write_json(run_dir / "iteration_summary.json", summaries)
    return summaries


def run(run_dir: Path, *, iterations: int) -> dict[str, Any]:
    sky_root = Path(os.environ.get("EE_SKYDISCOVER_ROOT", DEFAULT_SKY_ROOT)).resolve()
    sky_commit = _git(sky_root, "rev-parse", "HEAD")
    if sky_commit != EXPECTED_SKY_COMMIT:
        raise RuntimeError(f"SkyDiscover commit mismatch: {sky_commit}")
    if _git(sky_root, "status", "--short"):
        raise RuntimeError("SkyDiscover checkout is dirty")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"run directory must be new or empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim_scope": "SYNTHETIC_MECHANICS_ONLY",
        "explicit_non_claims": [
            "NO_REAL_BLINDASSIST_HEADROOM_CLAIM",
            "NO_SAFETY_CLAIM",
            "NO_DEPLOYMENT_CLAIM",
        ],
        "repo_commit": _git(REPO_ROOT, "rev-parse", "HEAD"),
        "sky_commit": sky_commit,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "iterations": iterations,
        "checkpoint_interval": 1,
        "development_seed": 2026081601,
        "task_files": {
            str(path.relative_to(REPO_ROOT)): _sha256(path)
            for path in (INITIAL, EVALUATOR, TASK_ROOT / "locked_eval.py", CONFIG)
        },
        "resources": _resource_receipt(),
    }
    _write_json(run_dir / "manifest.json", manifest)

    os.environ["EE_SFR_EVAL_LEDGER"] = str(run_dir / "evaluations.jsonl")
    os.environ["EE_SFR_EXPECTED_ITERATIONS"] = str(iterations)
    started = time.perf_counter()
    from skydiscover import run_discovery
    from skydiscover.config import LLMModelConfig, load_config

    config = load_config(CONFIG)
    config.max_iterations = iterations
    config.checkpoint_interval = 1
    model_config = LLMModelConfig(
        name=MODEL,
        temperature=0.0,
        max_tokens=32768,
        timeout=1800,
        retries=0,
        reasoning_effort=REASONING_EFFORT,
        init_client=lambda cfg: _SkyCodexClient(cfg, run_dir=run_dir),
    )
    config.llm.models = [model_config]
    config.llm.evaluator_models = [model_config]
    config.llm.guide_models = [model_config]
    result = run_discovery(
        evaluator=EVALUATOR,
        initial_program=INITIAL,
        iterations=iterations,
        config=config,
        output_dir=str(run_dir / "upstream"),
        cleanup=False,
    )
    wall_seconds = time.perf_counter() - started

    best_path = run_dir / "best_candidate.py"
    best_path.write_text(result.best_solution or INITIAL.read_text(encoding="utf-8"), encoding="utf-8")
    candidate_lock = {
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "candidate_path": str(best_path),
        "candidate_sha256": _sha256(best_path),
        "iterations_requested": iterations,
    }
    _write_json(run_dir / "candidate_lock.json", candidate_lock)

    from tasks.scalefree_risk_fusion_v0.locked_eval import score_without_record

    holdout_seed = secrets.randbits(63)
    _write_json(
        run_dir / "holdout_seed.json",
        {"generated_after_candidate_lock": True, "seed": holdout_seed},
    )
    holdout = score_without_record(str(best_path), seed=holdout_seed)
    summaries = _build_iteration_summary(run_dir)
    final = {
        "schema_version": "1.0",
        "scientific_outcome": "NOT_EVALUABLE_DATA",
        "mechanics_status": "PASS",
        "reason": "synthetic benchmark has no eligible real BlindAssist truth",
        "iterations_requested": iterations,
        "solution_evaluations_observed": sum(
            int(item["evox_iteration"] is not None) for item in summaries
        ),
        "best_candidate_sha256": _sha256(best_path),
        "upstream_best_score": result.best_score,
        "development_best": max(summaries, key=lambda item: item["combined_score"]),
        "synthetic_holdout": holdout,
        "wall_seconds": wall_seconds,
        "token_usage": _token_usage(run_dir),
    }
    _write_json(run_dir / "final_result.json", final)
    return final


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if args.iterations < 1:
        raise ValueError("iterations must be positive")
    result = run(args.run_dir.resolve(), iterations=args.iterations)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
