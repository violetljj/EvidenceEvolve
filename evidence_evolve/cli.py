from __future__ import annotations

import argparse
import json
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
from evidence_evolve.canary import replay_run, run_canary
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import (
    ContractValidationError,
    ProtocolLock,
    dump_contract,
    load_contract,
)
from evidence_evolve.models import CandidateGenome, GateVerdict, ResearchContract


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


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
    result = replay_run(Path(args.run_dir).resolve())
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


def command_explain(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    with sqlite3.connect(run_dir / "research.db") as connection:
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
    }
    for name, schema in schemas.items():
        (output / name).write_text(_json(schema) + "\n", encoding="utf-8")
    print(_json({"output_dir": str(output), "schemas": sorted(schemas)}))
    return 0


def command_backend_status(args: argparse.Namespace) -> int:
    print(
        _json(
            {
                "codex_cli": {"discovered": CodexCliBackend().available()},
                "shinka": ShinkaBackend().status().__dict__,
            }
        )
    )
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

    replay = subparsers.add_parser("replay", help="recompute verdicts from receipts")
    replay.add_argument("run_dir")
    replay.set_defaults(handler=command_replay)

    inspect = subparsers.add_parser("inspect", help="summarize a run archive")
    inspect.add_argument("run_dir")
    inspect.set_defaults(handler=command_inspect)

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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ValidationError, ContractValidationError, FileNotFoundError, ValueError) as exc:
        payload: dict[str, object] = {"error": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, ContractValidationError):
            payload["validation"] = exc.report.model_dump(mode="json")
        print(_json(payload), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

