from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.backends.codex_cli import CodexCliBackend
from evidence_evolve.backends.shinka_backend import ShinkaBackend
from evidence_evolve.canary import run_canary
from evidence_evolve.discovery.campaign import (
    CampaignCandidate,
    CampaignRunner,
    EvaluationRun,
)
from evidence_evolve.discovery.autonomous import (
    AutonomousCampaignRunner,
    AutonomousEvaluationContext,
    ImplementationManifest,
)
from evidence_evolve.discovery.director import ResearchAction, ResearchDirectorDecision
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import (
    ContractValidationError,
    ProtocolLock,
    dump_contract,
    load_contract,
)
from evidence_evolve.models import CandidateGenome, GateVerdict, ResearchContract
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_evolve.meta_evolution.promotion import (
    PolicyBenchmarkResult,
    PolicyPromotionProtocol,
    evaluate_policy_promotion,
)
from evidence_evolve.onnx_campaign import evaluate_onnx_candidate
from evidence_evolve.replay import replay_evaluation, replay_verdict
from evidence_evolve.research_memory import (
    MemoryKind,
    MemoryRole,
    RoleScopedMemoryPacket,
    ScientificMemoryCard,
)
from evidence_evolve.research_actions.intelligence import (
    LiteratureRepoIntelligenceExecutor,
)
from evidence_evolve.research_actions.models import (
    ActionState,
    ResearchActionJob,
    ResearchActionReceipt,
)
from evidence_evolve.research_actions.store import ResearchActionRunner
from evidence_evolve.budgets import BudgetLedger
from evidence_evolve.benchmarks.models import BenchmarkProtocol, BenchmarkSuiteResult
from evidence_evolve.benchmarks.protocol import (
    BenchmarkProtocolLock,
    BenchmarkProtocolValidation,
    dump_benchmark_protocol,
    load_benchmark_protocol,
)
from evidence_evolve.benchmarks.runner import ThreeArmBenchmarkRunner
from evidence_evolve.proposals.admission import run_mechanics_admission
from evidence_evolve.proposals.models import (
    MechanicsAdmissionProtocol,
    MechanicsAdmissionReceipt,
    ProposalIR,
    ProposalMaterializerMode,
)
from evidence_evolve.proposals.shinka_adapter import (
    run_official_shinka_with_materializer,
)
from evidence_evolve.proposals.p2_r1_execution import run_p2_r1_execution
from evidence_evolve.search.models import SearchRunReceipt, SearchRunRequest
from evidence_evolve.search.shinka_native import ShinkaNativeEngine


def _json(value: Any) -> str:
    def default(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        if isinstance(item, Path):
            return str(item)
        raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")

    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=default,
    )


def _repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return Path(completed.stdout.strip()).resolve()
    return Path.cwd().resolve()


def _load_candidate(path: Path) -> CandidateGenome:
    with path.open("r", encoding="utf-8") as stream:
        if path.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(stream)
        else:
            payload = json.load(stream)
    return CandidateGenome.model_validate(payload)


def _load_payload(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        if path.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(stream)
        return json.load(stream)


def _load_frozen_campaign_adapter(
    spec: str,
    *,
    contract: ResearchContract,
    repo: Path,
) -> Any:
    try:
        module_name, function_name = spec.split(":", 1)
    except ValueError as exc:
        raise ValueError("adapter must use module:function syntax") from exc
    module = importlib.import_module(module_name)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise ValueError(f"campaign adapter is not callable: {spec}")
    source = inspect.getsourcefile(function)
    if source is None:
        raise ValueError("campaign adapter must be backed by a repository file")
    try:
        relative = Path(source).resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("campaign adapter source is outside the repository") from exc
    frozen_adapters = {
        asset.path
        for asset in contract.frozen_assets
        if asset.kind.value == "adapter"
    }
    if relative not in frozen_adapters:
        raise ValueError(f"campaign adapter is not frozen by the contract: {relative}")
    return function


def _load_benchmark_adapter(
    spec: str,
    *,
    protocol: BenchmarkProtocol,
    repo: Path,
) -> Any:
    try:
        module_name, function_name = spec.split(":", 1)
    except ValueError as exc:
        raise ValueError("benchmark adapter must use module:function syntax") from exc
    module = importlib.import_module(module_name)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise ValueError(f"benchmark adapter is not callable: {spec}")
    source = inspect.getsourcefile(function)
    if source is None:
        raise ValueError("benchmark adapter must be backed by a repository file")
    try:
        relative = Path(source).resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("benchmark adapter source is outside the repository") from exc
    if relative not in {asset.path for asset in protocol.assets}:
        raise ValueError(f"benchmark adapter is not frozen by the protocol: {relative}")
    return function


def command_lock_contract(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    draft = load_contract(Path(args.contract))
    locked = ProtocolLock(repo).lock(draft)
    output = Path(args.output)
    dump_contract(locked, output)
    report = ProtocolLock(repo).validate(locked)
    print(_json({"output": str(output.resolve()), "validation": report}))
    return 0 if report.valid else 2


def command_validate_contract(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    contract = load_contract(Path(args.contract))
    report = ProtocolLock(repo).validate(contract)
    print(_json(report))
    return 0 if report.valid else 2


def command_audit_candidate(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    contract = load_contract(Path(args.contract))
    ProtocolLock(repo).assert_valid(contract)
    candidate = _load_candidate(Path(args.candidate))
    registry = ClosureRegistry.load(repo / contract.closure_registry)
    report = audit_candidate(
        contract,
        candidate,
        registry,
        changed_files=args.changed_file,
        verified_reopen_conditions=set(args.verified_reopen_condition or []),
    )
    print(_json(report))
    return 0 if report.valid else 3


def command_run_canary(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    contract_path = Path(args.contract).resolve()
    contract = load_contract(contract_path)
    run_dir = (
        Path(args.run_dir).resolve()
        if args.run_dir
        else repo / "runs" / contract.campaign.id
    )
    summary = run_canary(contract_path, repo, run_dir)
    print(_json({"run_dir": str(run_dir), **summary}))
    return 0


def command_replay(args: argparse.Namespace) -> int:
    result = replay_verdict(
        Path(args.run_dir).resolve(),
        _repo_root(args.repo),
    )
    print(_json(result))
    return 0 if result["passed"] else 4


def command_replay_evaluation(args: argparse.Namespace) -> int:
    result = replay_evaluation(
        Path(args.run_dir).resolve(),
        _repo_root(args.repo),
    )
    print(_json(result))
    return 0 if result["passed"] else 4


def command_inspect(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    summary = ArchiveStore(run_dir / "research.db").summary()
    canary_summary = run_dir / "canary_summary.json"
    if canary_summary.is_file():
        summary["canary"] = json.loads(canary_summary.read_text(encoding="utf-8"))
    print(_json(summary))
    return 0


def command_memory_query(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    packet = ArchiveStore(run_dir / "research.db").research_memory_packet(
        role=MemoryRole(args.role),
        query=args.query,
        campaign=args.campaign,
        family=args.family,
        kinds={MemoryKind(value) for value in args.kind} if args.kind else None,
        limit=args.limit,
    )
    print(_json(packet))
    return 0


def command_search_literature(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    run_dir = Path(args.run_dir).resolve()
    contract_path = run_dir / "contract.locked.yaml"
    if not contract_path.is_file():
        raise FileNotFoundError(
            f"run directory is not bound to a locked contract: {contract_path}"
        )
    contract = load_contract(contract_path)
    ProtocolLock(repo).assert_valid(contract)
    database = run_dir / "research.db"
    ArchiveStore(database)
    budgets = BudgetLedger(database, contract.budgets)
    action_id = args.action_id or (
        "INTEL-" + hashlib.sha256(args.query.encode("utf-8")).hexdigest()[:20]
    )
    executor = LiteratureRepoIntelligenceExecutor(
        run_dir=run_dir,
        openalex_api_key=os.environ.get(args.openalex_api_key_env),
        github_token=os.environ.get(args.github_token_env),
    )
    result = ResearchActionRunner(
        database=database,
        run_dir=run_dir,
        budgets=budgets,
    ).run(
        ResearchActionJob(
            action_id=action_id,
            campaign_id=contract.campaign.id,
            action=ResearchAction.SEARCH_LITERATURE,
            query=args.query,
            max_papers=args.max_papers,
            max_repositories=args.max_repositories,
            max_source_files_per_repository=args.max_source_files,
        ),
        executor,
    )
    print(_json(result))
    if result.state is ActionState.WAITING_FOR_AUTHORITY:
        return 6
    if result.state is ActionState.FAILED:
        return 7
    return 0


def command_explain(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    with sqlite3.connect(run_dir / "research.db") as connection:
        has_receipts = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='evaluation_receipts'"
        ).fetchone()
        row = None
        if has_receipts:
            row = connection.execute(
                "SELECT receipt_path FROM evaluation_receipts WHERE candidate_id = ? "
                "ORDER BY created_at_utc DESC LIMIT 1",
                (args.candidate_id,),
            ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT receipt_path FROM candidates WHERE candidate_id = ?",
                (args.candidate_id,),
            ).fetchone()
    if row is None:
        print(_json({"error": "candidate not found", "candidate_id": args.candidate_id}))
        return 5
    payload = json.loads((run_dir / row[0]).read_text(encoding="utf-8"))
    receipt = payload["receipt"]
    print(
        _json(
            {
                "candidate_id": args.candidate_id,
                "decision": receipt["verdict"]["decision"],
                "scientific_outcome": receipt["verdict"]["scientific_outcome"],
                "archive_class": receipt["verdict"]["archive_class"],
                "reasons": receipt["verdict"]["reasons"],
                "constraint_checks": receipt["verdict"]["constraint_checks"],
                "controls_complete": receipt["verdict"]["controls_complete"],
            }
        )
    )
    return 0


def command_export_schemas(args: argparse.Namespace) -> int:
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    schemas = {
        "research_contract.schema.json": ResearchContract.model_json_schema(),
        "candidate.schema.json": CandidateGenome.model_json_schema(),
        "verdict.schema.json": GateVerdict.model_json_schema(),
        "research_policy.schema.json": ResearchPolicyGenome.model_json_schema(),
        "campaign_candidate.schema.json": CampaignCandidate.model_json_schema(),
        "implementation_manifest.schema.json": (
            ImplementationManifest.model_json_schema()
        ),
        "evaluation_run.schema.json": EvaluationRun.model_json_schema(),
        "policy_benchmark.schema.json": PolicyBenchmarkResult.model_json_schema(),
        "benchmark_protocol.schema.json": BenchmarkProtocol.model_json_schema(),
        "benchmark_suite_result.schema.json": BenchmarkSuiteResult.model_json_schema(),
        "benchmark_protocol_validation.schema.json": (
            BenchmarkProtocolValidation.model_json_schema()
        ),
        "search_run_request.schema.json": SearchRunRequest.model_json_schema(),
        "search_run_receipt.schema.json": SearchRunReceipt.model_json_schema(),
        "proposal_ir.schema.json": ProposalIR.model_json_schema(),
        "mechanics_admission_protocol.schema.json": (
            MechanicsAdmissionProtocol.model_json_schema()
        ),
        "mechanics_admission_receipt.schema.json": (
            MechanicsAdmissionReceipt.model_json_schema()
        ),
        "policy_promotion_protocol.schema.json": (
            PolicyPromotionProtocol.model_json_schema()
        ),
        "scientific_memory_card.schema.json": ScientificMemoryCard.model_json_schema(),
        "role_scoped_memory_packet.schema.json": RoleScopedMemoryPacket.model_json_schema(),
        "research_director_decision.schema.json": (
            ResearchDirectorDecision.model_json_schema()
        ),
        "research_action_job.schema.json": ResearchActionJob.model_json_schema(),
        "research_action_receipt.schema.json": (
            ResearchActionReceipt.model_json_schema()
        ),
    }
    for name, schema in schemas.items():
        (output / name).write_text(_json(schema) + "\n", encoding="utf-8")
    print(_json({"output_dir": str(output), "schemas": sorted(schemas)}))
    return 0


def command_backend_status(args: argparse.Namespace) -> int:
    print(
        _json(
            {
                "codex_cli": CodexCliBackend().status(),
                "shinka": ShinkaBackend().status().__dict__,
            }
        )
    )
    return 0


def command_evaluate_onnx_candidate(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    result = evaluate_onnx_candidate(
        Path(args.contract).resolve(),
        Path(args.proposal).resolve(),
        repo,
        Path(args.worktree).resolve(),
        Path(args.run_dir).resolve(),
        confirmation=args.confirmation,
    )
    print(_json(result))
    return 0


def command_campaign_run(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    contract = load_contract(Path(args.contract).resolve())
    ProtocolLock(repo).assert_valid(contract)
    policy = ResearchPolicyGenome.model_validate(
        _load_payload(Path(args.policy).resolve())
    )
    payload = _load_payload(Path(args.pool).resolve())
    if not isinstance(payload, dict):
        raise ValueError("campaign pool must be an object")
    generation_id = str(payload.get("generation_id", "")).strip()
    if not generation_id:
        raise ValueError("campaign pool requires generation_id")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("campaign pool requires a candidates list")
    candidates = [CampaignCandidate.model_validate(item) for item in raw_candidates]
    adapter = _load_frozen_campaign_adapter(
        args.adapter,
        contract=contract,
        repo=repo,
    )

    def evaluate(item: CampaignCandidate) -> EvaluationRun:
        return EvaluationRun.model_validate(adapter(item))

    runner = CampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(repo / contract.closure_registry),
        policy=policy,
        run_dir=Path(args.run_dir).resolve(),
    )
    result = runner.run_generation(
        generation_id=generation_id,
        candidates=candidates,
        evaluate=evaluate,
        max_evaluations=args.max_evaluations,
        signature_tolerance=args.signature_tolerance,
    )
    print(_json(result))
    return 0


def command_campaign_autonomous(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    contract = load_contract(Path(args.contract).resolve())
    ProtocolLock(repo).assert_valid(contract)
    policy = ResearchPolicyGenome.model_validate(
        _load_payload(Path(args.policy).resolve())
    )
    adapter = _load_frozen_campaign_adapter(
        args.adapter,
        contract=contract,
        repo=repo,
    )
    reference_metrics = (
        _load_payload(Path(args.reference_metrics).resolve())
        if args.reference_metrics
        else {}
    )
    if not isinstance(reference_metrics, dict) or any(
        not isinstance(value, (int, float)) for value in reference_metrics.values()
    ):
        raise ValueError("reference metrics must be a JSON/YAML object of numbers")

    def evaluate(context: AutonomousEvaluationContext) -> EvaluationRun:
        return EvaluationRun.model_validate(adapter(context))

    backend = CodexCliBackend(args.codex_executable)
    status = backend.status()
    if not status["usable"]:
        raise RuntimeError(
            "Codex CLI is not usable; pass --codex-executable with a working "
            f"Codex CLI path. Backend status: {status.get('error')}"
        )
    run_dir = Path(args.run_dir).resolve()
    intelligence_executor = (
        LiteratureRepoIntelligenceExecutor(
            run_dir=run_dir,
            openalex_api_key=os.environ.get(args.openalex_api_key_env),
            github_token=os.environ.get(args.github_token_env),
        )
        if args.enable_live_intelligence
        else None
    )
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry.load(repo / contract.closure_registry),
        policy=policy,
        repo_root=repo,
        run_dir=run_dir,
        evaluate=evaluate,
        backend=backend,
        worktree_root=(
            Path(args.worktree_root).resolve() if args.worktree_root else None
        ),
        reference_metrics={
            str(key): float(value) for key, value in reference_metrics.items()
        },
        intelligence_executor=intelligence_executor,
        memory_enabled=not args.disable_scientific_memory,
        timeout_seconds=args.timeout_seconds,
    )
    result = runner.run(
        generations=args.generations,
        proposals_per_generation=args.proposals_per_generation,
        max_evaluations_per_generation=args.max_evaluations_per_generation,
        generation_prefix=args.generation_prefix,
        signature_tolerance=args.signature_tolerance,
    )
    print(_json(result))
    return 0


def command_policy_evaluate_promotion(args: argparse.Namespace) -> int:
    baseline = PolicyBenchmarkResult.model_validate(
        _load_payload(Path(args.baseline).resolve())
    )
    candidate = PolicyBenchmarkResult.model_validate(
        _load_payload(Path(args.candidate).resolve())
    )
    protocol = (
        PolicyPromotionProtocol.model_validate(
            _load_payload(Path(args.protocol).resolve())
        )
        if args.protocol
        else PolicyPromotionProtocol()
    )
    print(
        _json(
            evaluate_policy_promotion(
                candidate=candidate,
                baseline=baseline,
                protocol=protocol,
            )
        )
    )
    return 0


def command_benchmark_lock_protocol(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    draft = load_benchmark_protocol(Path(args.draft))
    locked = BenchmarkProtocolLock(repo).lock(draft)
    output = Path(args.output)
    dump_benchmark_protocol(locked, output)
    report = BenchmarkProtocolLock(repo).validate(locked)
    print(_json({"output": str(output.resolve()), "validation": report}))
    return 0 if report.valid else 2


def command_benchmark_validate_protocol(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    protocol = load_benchmark_protocol(Path(args.protocol))
    report = BenchmarkProtocolLock(repo).validate(protocol)
    print(_json(report))
    return 0 if report.valid else 2


def command_benchmark_run_graph_coloring(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    protocol = load_benchmark_protocol(Path(args.protocol))
    adapter_spec = args.adapter or protocol.arm_adapter
    if adapter_spec != protocol.arm_adapter:
        raise ValueError(
            "benchmark adapter override does not match the frozen protocol: "
            f"expected={protocol.arm_adapter} actual={adapter_spec}"
        )
    adapter = _load_benchmark_adapter(adapter_spec, protocol=protocol, repo=repo)
    result = ThreeArmBenchmarkRunner(
        protocol=protocol,
        repo_root=repo,
        run_dir=Path(args.run_dir),
        adapter=adapter,
    ).run()
    print(_json(result))
    return 0


def command_search_shinka_native(args: argparse.Namespace) -> int:
    request = SearchRunRequest(
        run_id=args.run_id,
        task_dir=Path(args.task_dir),
        results_dir=Path(args.results_dir),
        num_generations=args.num_generations,
        config_fname=args.config_fname,
        set_overrides=args.set_overrides,
        max_evaluation_jobs=args.max_evaluation_jobs,
        max_proposal_jobs=args.max_proposal_jobs,
        max_db_workers=args.max_db_workers,
        verbose=args.verbose,
        debug=args.debug,
        proposal_materializer=args.proposal_materializer,
    )
    print(_json(ShinkaNativeEngine().run(request)))
    return 0


def command_search_shinka_official_materialized(args: argparse.Namespace) -> int:
    ShinkaBackend().require()
    upstream_args = list(args.upstream_args)
    if upstream_args and upstream_args[0] == "--":
        upstream_args = upstream_args[1:]
    return run_official_shinka_with_materializer(upstream_args)


def command_search_mechanics_admission(args: argparse.Namespace) -> int:
    receipt = run_mechanics_admission(
        protocol_path=Path(args.protocol),
        repo=_repo_root(args.repo),
        run_dir=Path(args.run_dir),
        max_workers=args.max_workers,
        python_executable=args.python_executable,
    )
    print(_json(receipt))
    return 0 if receipt.admitted_for_expensive_search else 8


def command_search_p2_r1(args: argparse.Namespace) -> int:
    result = run_p2_r1_execution(
        repo=_repo_root(args.repo),
        protocol_path=Path(args.protocol),
        run_root=Path(args.run_root),
        dry_run=args.dry_run,
        max_parallel_blocks=args.max_parallel_blocks,
    )
    print(_json(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolve",
        description="Evidence-gated reproducible algorithm discovery harness",
    )
    parser.add_argument("--repo", help="Git repository root (default: auto-detect)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock = subparsers.add_parser("lock-contract", help="freeze commit and asset hashes")
    lock.add_argument("contract")
    lock.add_argument("--output", required=True)
    lock.set_defaults(handler=command_lock_contract)

    validate = subparsers.add_parser(
        "validate-contract", help="validate a locked research contract"
    )
    validate.add_argument("contract")
    validate.set_defaults(handler=command_validate_contract)

    audit = subparsers.add_parser(
        "audit-candidate", help="audit scope and closure eligibility"
    )
    audit.add_argument("contract")
    audit.add_argument("candidate")
    audit.add_argument("--changed-file", action="append")
    audit.add_argument("--verified-reopen-condition", action="append")
    audit.set_defaults(handler=command_audit_candidate)

    canary = subparsers.add_parser(
        "run-canary", help="run deterministic Harness trap scenarios"
    )
    canary.add_argument("contract")
    canary.add_argument("--run-dir")
    canary.set_defaults(handler=command_run_canary)

    replay = subparsers.add_parser(
        "replay", help="validate receipt bindings and recompute verdicts only"
    )
    replay.add_argument("run_dir")
    replay.set_defaults(handler=command_replay)

    replay_eval = subparsers.add_parser(
        "replay-evaluation",
        help="re-run the frozen evaluator and recompute bound verdicts",
    )
    replay_eval.add_argument("run_dir")
    replay_eval.set_defaults(handler=command_replay_evaluation)

    inspect = subparsers.add_parser("inspect", help="summarize a run archive")
    inspect.add_argument("run_dir")
    inspect.set_defaults(handler=command_inspect)

    memory_query = subparsers.add_parser(
        "memory-query",
        help="query evidence-bound research memory with a role firewall",
    )
    memory_query.add_argument("run_dir")
    memory_query.add_argument(
        "--role",
        choices=[role.value for role in MemoryRole],
        default=MemoryRole.RESEARCH_DIRECTOR.value,
    )
    memory_query.add_argument("--query")
    memory_query.add_argument("--campaign")
    memory_query.add_argument("--family")
    memory_query.add_argument(
        "--kind", action="append", choices=[kind.value for kind in MemoryKind]
    )
    memory_query.add_argument("--limit", type=int, default=8)
    memory_query.set_defaults(handler=command_memory_query)

    research_action = subparsers.add_parser(
        "research-action", help="execute a source-bound non-code research action"
    )
    research_action_commands = research_action.add_subparsers(
        dest="research_action_command", required=True
    )
    literature = research_action_commands.add_parser(
        "search-literature",
        help="search papers and inspect pinned public repositories",
    )
    literature.add_argument("run_dir")
    literature.add_argument("--query", required=True)
    literature.add_argument("--action-id")
    literature.add_argument("--max-papers", type=int, default=5)
    literature.add_argument("--max-repositories", type=int, default=2)
    literature.add_argument("--max-source-files", type=int, default=3)
    literature.add_argument("--openalex-api-key-env", default="OPENALEX_API_KEY")
    literature.add_argument("--github-token-env", default="GITHUB_TOKEN")
    literature.set_defaults(handler=command_search_literature)

    explain = subparsers.add_parser("explain", help="explain one deterministic verdict")
    explain.add_argument("run_dir")
    explain.add_argument("candidate_id")
    explain.set_defaults(handler=command_explain)

    schemas = subparsers.add_parser(
        "export-schemas", help="export role and contract JSON Schemas"
    )
    schemas.add_argument("--output-dir", default="schemas")
    schemas.set_defaults(handler=command_export_schemas)

    status = subparsers.add_parser(
        "backend-status", help="report optional backend availability"
    )
    status.set_defaults(handler=command_backend_status)

    onnx_candidate = subparsers.add_parser(
        "evaluate-onnx-candidate",
        help="evaluate one isolated ONNX rewrite candidate",
    )
    onnx_candidate.add_argument("contract")
    onnx_candidate.add_argument("proposal")
    onnx_candidate.add_argument("--worktree", required=True)
    onnx_candidate.add_argument("--run-dir", required=True)
    onnx_candidate.add_argument("--confirmation", action="store_true")
    onnx_candidate.set_defaults(handler=command_evaluate_onnx_candidate)

    campaign = subparsers.add_parser(
        "campaign", help="run or resume an R1 evidence-guided generation"
    )
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    for command_name in ("run", "resume"):
        campaign_run = campaign_commands.add_parser(
            command_name,
            help=f"{command_name} a generation through a frozen task adapter",
        )
        campaign_run.add_argument("contract")
        campaign_run.add_argument("pool")
        campaign_run.add_argument("--policy", required=True)
        campaign_run.add_argument("--adapter", required=True)
        campaign_run.add_argument("--run-dir", required=True)
        campaign_run.add_argument("--max-evaluations", type=int)
        campaign_run.add_argument("--signature-tolerance", type=float, default=0.0)
        campaign_run.set_defaults(handler=command_campaign_run)

    autonomous = campaign_commands.add_parser(
        "autonomous",
        help="run or resume a bounded multi-generation Codex discovery loop",
    )
    autonomous.add_argument("contract")
    autonomous.add_argument("--policy", required=True)
    autonomous.add_argument("--adapter", required=True)
    autonomous.add_argument("--run-dir", required=True)
    autonomous.add_argument("--generations", type=int, default=2)
    autonomous.add_argument("--proposals-per-generation", type=int, default=1)
    autonomous.add_argument("--max-evaluations-per-generation", type=int)
    autonomous.add_argument("--generation-prefix", default="GEN")
    autonomous.add_argument("--signature-tolerance", type=float, default=0.0)
    autonomous.add_argument("--reference-metrics")
    autonomous.add_argument("--timeout-seconds", type=int, default=1800)
    autonomous.add_argument("--codex-executable", default="codex")
    autonomous.add_argument("--worktree-root")
    autonomous.add_argument("--enable-live-intelligence", action="store_true")
    autonomous.add_argument(
        "--disable-scientific-memory",
        action="store_true",
        help="run the population/search loop with empty research-memory packets",
    )
    autonomous.add_argument("--openalex-api-key-env", default="OPENALEX_API_KEY")
    autonomous.add_argument("--github-token-env", default="GITHUB_TOKEN")
    autonomous.set_defaults(handler=command_campaign_autonomous)

    benchmark = subparsers.add_parser(
        "benchmark", help="run claim-bounded comparative benchmarks"
    )
    benchmark_commands = benchmark.add_subparsers(
        dest="benchmark_command", required=True
    )
    benchmark_lock = benchmark_commands.add_parser(
        "lock-protocol", help="hash and freeze a benchmark protocol and its assets"
    )
    benchmark_lock.add_argument("draft")
    benchmark_lock.add_argument("--output", required=True)
    benchmark_lock.set_defaults(handler=command_benchmark_lock_protocol)
    benchmark_validate = benchmark_commands.add_parser(
        "validate-protocol", help="validate a frozen benchmark protocol"
    )
    benchmark_validate.add_argument("protocol")
    benchmark_validate.set_defaults(handler=command_benchmark_validate_protocol)
    benchmark_run = benchmark_commands.add_parser(
        "run-graph-coloring", help="run the paired graph-coloring three-arm suite"
    )
    benchmark_run.add_argument("protocol")
    benchmark_run.add_argument("--run-dir", required=True)
    benchmark_run.add_argument(
        "--adapter",
        help="must match the adapter frozen in the benchmark protocol",
    )
    benchmark_run.set_defaults(handler=command_benchmark_run_graph_coloring)
    search = subparsers.add_parser(
        "search", help="run an upstream or experimental search engine"
    )
    search_commands = search.add_subparsers(dest="search_command", required=True)
    shinka_native = search_commands.add_parser(
        "shinka-native",
        help="run pinned Shinka search, optionally with shared proposal materialization",
    )
    shinka_native.add_argument("--run-id", required=True)
    shinka_native.add_argument("--task-dir", required=True)
    shinka_native.add_argument("--results-dir", required=True)
    shinka_native.add_argument("--num-generations", type=int, required=True)
    shinka_native.add_argument("--config-fname")
    shinka_native.add_argument(
        "--set", dest="set_overrides", action="append", default=[]
    )
    shinka_native.add_argument("--max-evaluation-jobs", type=int)
    shinka_native.add_argument("--max-proposal-jobs", type=int)
    shinka_native.add_argument("--max-db-workers", type=int)
    shinka_native.add_argument(
        "--verbose", action=argparse.BooleanOptionalAction, default=None
    )
    shinka_native.add_argument("--debug", action="store_true")
    shinka_native.add_argument(
        "--proposal-materializer",
        type=ProposalMaterializerMode,
        choices=list(ProposalMaterializerMode),
        default=ProposalMaterializerMode.UPSTREAM_STRICT,
    )
    shinka_native.set_defaults(handler=command_search_shinka_native)
    shinka_official = search_commands.add_parser(
        "shinka-official-materialized",
        help="run official Shinka with the shared EvidenceEvolve materializer",
    )
    shinka_official.add_argument("upstream_args", nargs=argparse.REMAINDER)
    shinka_official.set_defaults(
        handler=command_search_shinka_official_materialized
    )
    mechanics_admission = search_commands.add_parser(
        "mechanics-admission",
        help="replay frozen proposals through the candidate survival funnel",
    )
    mechanics_admission.add_argument("protocol")
    mechanics_admission.add_argument("--run-dir", required=True)
    mechanics_admission.add_argument("--max-workers", type=int)
    mechanics_admission.add_argument(
        "--python-executable", default=sys.executable
    )
    mechanics_admission.set_defaults(handler=command_search_mechanics_admission)
    p2_r1 = search_commands.add_parser(
        "p2-r1",
        help="run or dry-run the frozen P2-R1 Official/Native schedule",
    )
    p2_r1.add_argument(
        "--protocol",
        default="research/parity/shinka_native_p2_r1.protocol.json",
    )
    p2_r1.add_argument("--run-root", required=True)
    p2_r1.add_argument("--dry-run", action="store_true")
    p2_r1.add_argument("--max-parallel-blocks", type=int)
    p2_r1.set_defaults(handler=command_search_p2_r1)
    policy = subparsers.add_parser(
        "policy", help="evaluate research-policy evidence without auto-promoting"
    )
    policy_commands = policy.add_subparsers(dest="policy_command", required=True)
    promotion = policy_commands.add_parser(
        "evaluate-promotion",
        help="compare a policy candidate on a blind held-out meta suite",
    )
    promotion.add_argument("baseline")
    promotion.add_argument("candidate")
    promotion.add_argument("--protocol")
    promotion.set_defaults(handler=command_policy_evaluate_promotion)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ValidationError,
        ContractValidationError,
        FileNotFoundError,
        RuntimeError,
        ValueError,
    ) as exc:
        payload: dict[str, object] = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, ContractValidationError):
            payload["validation"] = exc.report.model_dump(mode="json")
        print(_json(payload), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
