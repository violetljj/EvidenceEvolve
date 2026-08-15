from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from evidence_evolve.discovery.autonomous import (
    AutonomousCampaignRunner,
    AutonomousEvaluationContext,
)
from evidence_evolve.discovery.campaign import CampaignCandidate, EvaluationRun
from evidence_evolve.discovery.population import DuplicateCandidateCodeError
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.governance.protocol_lock import load_contract
from evidence_evolve.hashing import sha256_object
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_evolve.models import (
    Budgets,
    ContractLock,
    EvaluationInput,
    MechanicsStatus,
    MutationType,
    ScientificOutcome,
)
from evidence_evolve.research_actions.intelligence import (
    LiteratureRepoIntelligenceExecutor,
)
from evidence_evolve.research_memory import MemoryRole


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class FakeCodexBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.proposal_count = 0
        self.inherited_values: list[str] = []

    def run(
        self,
        *,
        role,
        prompt: str,
        workdir: Path,
        output_schema: Path,
        output_path: Path,
        events_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> dict[str, object]:
        del events_path, stderr_path, timeout_seconds
        self.calls.append({"role": role.name, "prompt": prompt, "workdir": workdir})
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if role.name == "hypothesis_explorer":
            self.proposal_count += 1
            schema = json.loads(output_schema.read_text(encoding="utf-8"))
            candidate_id = schema["$defs"]["CandidateGenome"]["properties"][
                "candidate_id"
            ]["const"]
            genome_properties = schema["$defs"]["CandidateGenome"]["properties"]
            parent = genome_properties["genetic_parent_id"]["enum"][0]
            island = genome_properties["island"]["const"]
            mutation_type = genome_properties["mutation_type"]["const"]
            payload = {
                "acquisition": {
                    "candidate": {
                        "candidate_id": candidate_id,
                        "parent_ids": [parent],
                        "genetic_parent_id": parent,
                        "island": island,
                        "family": f"family-{self.proposal_count}",
                        "mutation_type": mutation_type,
                        "hypothesis": "The bounded candidate change improves clearance metric.",
                        "intervention": "Change the candidate value for this generation.",
                        "expected_signature": {
                            "improve": ["clearance_mae_delta"],
                            "unchanged": ["false_block_delta_pp"],
                        },
                        "falsifier": "The measured clearance metric does not improve.",
                        "required_controls": ["wrong_factor", "zero_factor"],
                        "editable_files": ["candidates/model.py"],
                        "estimated_cost_tier": 1,
                    },
                    "signals": {
                        "admit_probability": 0.8,
                        "expected_improvement": 0.2,
                        "information_gain": 0.7,
                        "novelty": 0.6,
                    },
                }
            }
        else:
            candidate_id = next(
                token.strip('"{},')
                for token in prompt.split()
                if token.startswith('"GEN-')
            )
            self.inherited_values.append(
                (workdir / "candidates" / "model.py").read_text(encoding="utf-8")
            )
            (workdir / "candidates" / "model.py").write_text(
                f"VALUE = '{candidate_id}'\n", encoding="utf-8"
            )
            payload = {
                "status": "IMPLEMENTED",
                "summary": f"implemented {candidate_id}",
                "tests": ["focused fake check"],
            }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return {"status": "PASS", "command": ["fake-codex"]}


def test_two_generation_loop_uses_feedback_and_resumes_idempotently(
    tmp_path, contract
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    candidate_file = repo / "candidates" / "model.py"
    candidate_file.parent.mkdir(parents=True)
    candidate_file.write_text("VALUE = 'seed'\n", encoding="utf-8")
    prompts = repo / "prompts"
    prompts.mkdir()
    (prompts / "explorer.md").write_text("Explore safely.\n", encoding="utf-8")
    (prompts / "implementer.md").write_text(
        "Implement safely.\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "baseline",
    )
    base_commit = _git(repo, "rev-parse", "HEAD")
    contract = contract.model_copy(deep=True)
    contract.campaign.base_commit = base_commit
    contract.budgets = Budgets(
        proposal_calls=2,
        implementations=2,
        mechanics_runs=2,
    )
    contract.lock = ContractLock(
        content_sha256=sha256_object(
            contract.model_dump(mode="python", exclude={"lock"})
        )
    )
    run_dir = tmp_path / "run"

    def evaluate(context: AutonomousEvaluationContext) -> EvaluationRun:
        value = (context.worktree / "candidates" / "model.py").read_text(
            encoding="utf-8"
        )
        assert context.candidate.acquisition.candidate.candidate_id in value
        candidate = context.candidate.acquisition.candidate
        changed = ["candidates/model.py"]
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=contract.lock.content_sha256,
                candidate=candidate,
                stage=context.candidate.stage,
                changed_files=changed,
                mechanics_status=MechanicsStatus.PASS,
                data_eligible=True,
                metrics={
                    "clearance_mae_delta": -0.1,
                    "false_block_delta_pp": 0.0,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=(
                    ScientificOutcome.VALID_NEGATIVE
                    if candidate.candidate_id == "GEN-001-C01"
                    else ScientificOutcome.POSITIVE_HEADROOM
                ),
            ),
            command=["fake-evaluator"],
            elapsed_seconds=0.01,
            candidate_commit=_git(context.worktree, "rev-parse", "HEAD"),
        )

    backend = FakeCodexBackend()
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(
            policy_id="POLICY-AUTO",
            stagnation_generations=1,
        ),
        repo_root=repo,
        run_dir=run_dir,
        evaluate=evaluate,
        backend=backend,
        reference_metrics={
            "clearance_mae_delta": 0.0,
            "false_block_delta_pp": 0.0,
        },
    )
    result = runner.run(generations=2)
    bound_contract = load_contract(run_dir / "contract.locked.yaml")
    assert bound_contract == contract
    assert json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8")) == {
        "base_commit": base_commit,
        "campaign_id": contract.campaign.id,
        "claim_scope": contract.campaign.claim_scope,
        "contract_sha256": contract.lock.content_sha256,
        "policy_id": "POLICY-AUTO",
        "policy_sha256": hashlib.sha256(
            runner.policy.model_dump_json().encode("utf-8")
        ).hexdigest(),
        "scientific_memory_enabled": True,
    }
    assert len(result.generations) == 2
    assert result.generations[1].evaluations[0].candidate_id == "GEN-002-C01"
    second_proposal_prompt = [
        call["prompt"]
        for call in backend.calls
        if call["role"] == "hypothesis_explorer"
    ][1]
    assert "GEN-001-C01" in second_proposal_prompt
    assert "KILL" in second_proposal_prompt
    assert "Discovery mode: BREAKTHROUGH" in second_proposal_prompt
    assert "GEN-001-C01" in backend.inherited_values[1]
    assert [trace.mode.value for trace in result.policy_effect_traces] == [
        "NORMAL",
        "BREAKTHROUGH",
    ]
    assert result.policy_effect_traces[0].island_assignments == {
        "GEN-001-C01": "main"
    }
    assert result.policy_effect_traces[1].parent_pools_by_island == {
        "main": ["GEN-001-C01"]
    }
    assert "FAILURE" in result.policy_effect_traces[1].parent_roles[
        "GEN-001-C01"
    ]
    assert (
        result.policy_effect_traces[1].mutation_assignments["GEN-002-C01"]
        is MutationType.CROSS_FAMILY
    )
    receipt_payload = json.loads(
        (
            run_dir
            / "candidates"
            / "GEN-002-C01"
            / "receipts"
            / "GEN-002.M0_MECHANICS.json"
        ).read_text(encoding="utf-8")
    )["receipt"]
    assert receipt_payload["genetic_parent_id"] == "GEN-001-C01"
    assert (
        receipt_payload["genetic_parent_commit"]
        == result.generations[0].evaluations[0].candidate_commit
    )
    assert receipt_payload["parent_patch_sha256"]
    assert receipt_payload["candidate_ref"].startswith("refs/evidence-evolve/")
    assert (
        _git(repo, "rev-parse", receipt_payload["candidate_ref"])
        == result.generations[1].evaluations[0].candidate_commit
    )
    assert result.budgets["proposal_calls"]["used"] == 2
    assert result.budgets["implementations"]["used"] == 2
    assert result.budgets["mechanics_runs"]["used"] == 2

    resume_backend = FakeCodexBackend()
    resumed = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(
            policy_id="POLICY-AUTO",
            stagnation_generations=1,
        ),
        repo_root=repo,
        run_dir=run_dir,
        evaluate=evaluate,
        backend=resume_backend,
        reference_metrics={
            "clearance_mae_delta": 0.0,
            "false_block_delta_pp": 0.0,
        },
    ).run(generations=2)
    assert resume_backend.calls == []
    assert all(
        generation.evaluations[0].resumed for generation in resumed.generations
    )
    assert resumed.budgets == result.budgets
    assert resumed.population == result.population


def test_live_intelligence_waits_before_proposals_when_authority_is_missing(
    tmp_path, contract
) -> None:
    contract = contract.model_copy(deep=True)
    contract.budgets = Budgets(
        proposal_calls=1,
        implementations=1,
        mechanics_runs=1,
        literature_searches=1,
        repository_inspections=2,
    )
    contract.lock = ContractLock(
        content_sha256=sha256_object(
            contract.model_dump(mode="python", exclude={"lock"})
        )
    )
    run_dir = tmp_path / "run"
    backend = FakeCodexBackend()
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-INTELLIGENCE"),
        repo_root=tmp_path,
        run_dir=run_dir,
        evaluate=lambda context: None,  # type: ignore[arg-type,return-value]
        backend=backend,
        intelligence_executor=LiteratureRepoIntelligenceExecutor(
            run_dir=run_dir,
            openalex_api_key=None,
        ),
    )

    with pytest.raises(RuntimeError, match="waiting for authority"):
        runner.run(generations=1)

    assert backend.calls == []
    assert not (
        run_dir / "generations" / "GEN-001" / "research_director_decision.json"
    ).exists()
    assert runner.budgets.snapshot()["literature_searches"]["used"] == 0


def test_proposal_schema_binds_candidate_and_parent_ids(contract, tmp_path) -> None:
    contract = contract.model_copy(deep=True)
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-AUTO"),
        repo_root=tmp_path,
        run_dir=tmp_path / "run",
        evaluate=lambda context: None,  # type: ignore[arg-type,return-value]
        backend=FakeCodexBackend(),
    )
    schema = runner._proposal_schema(
        "GEN-009-C03",
        "test",
        ["PARENT-1"],
        MutationType.MECHANISM,
    )
    genome = schema["$defs"]["CandidateGenome"]
    assert genome["properties"]["candidate_id"]["const"] == "GEN-009-C03"
    assert genome["properties"]["island"] == {
        "const": "test",
        "type": "string",
    }
    assert genome["properties"]["parent_ids"]["items"] == {
        "enum": ["PARENT-1"],
        "type": "string",
    }
    assert genome["properties"]["genetic_parent_id"] == {
        "enum": ["PARENT-1"],
        "type": "string",
    }
    assert genome["properties"]["mutation_type"] == {
        "const": "mechanism_mutation",
        "type": "string",
    }
    assert genome["properties"]["required_controls"] == {
        "items": {
            "enum": ["wrong_factor", "zero_factor"],
            "type": "string",
        },
        "maxItems": 2,
        "minItems": 2,
        "type": "array",
    }
    signature = schema["$defs"]["ExpectedSignature"]["properties"]
    assert signature["improve"]["items"]["enum"] == [
        "clearance_mae_delta",
        "false_block_delta_pp",
    ]
    assert signature["unchanged"]["items"]["enum"] == [
        "clearance_mae_delta",
        "false_block_delta_pp",
    ]
    assert schema["properties"]["reference_metrics"] == {
        "additionalProperties": False,
        "properties": {},
        "required": [],
        "type": "object",
    }
    assert schema["properties"]["stage"]["const"] == "M0_MECHANICS"
    acquisition = schema["$defs"]["CandidateAcquisition"]
    assert acquisition["properties"]["verified_reopen_conditions"]["maxItems"] == 0

    unsupported = {
        "contains",
        "default",
        "maxContains",
        "maxLength",
        "maxProperties",
        "minContains",
        "minLength",
        "minProperties",
        "title",
        "uniqueItems",
    }

    def assert_codex_strict(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                assert_codex_strict(item)
            return
        if not isinstance(node, dict):
            return
        assert unsupported.isdisjoint(node)
        if node.get("type") == "object":
            properties = node.get("properties")
            assert isinstance(properties, dict)
            assert node.get("additionalProperties") is False
            assert node.get("required") == list(properties)
        for value in node.values():
            assert_codex_strict(value)

    assert_codex_strict(schema)
    assert_codex_strict(runner._implementation_schema())
    assert runner._implementation_schema()["required"] == [
        "status",
        "summary",
        "tests",
    ]


def test_memory_disabled_profile_returns_empty_packets_without_retrieval_events(
    tmp_path, contract
) -> None:
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-NO-MEMORY"),
        repo_root=tmp_path,
        run_dir=tmp_path / "run",
        evaluate=lambda context: None,  # type: ignore[arg-type,return-value]
        backend=FakeCodexBackend(),
        memory_enabled=False,
    )

    packet = runner._memory_packet(
        generation_id="GEN-002",
        role=MemoryRole.HYPOTHESIS_EXPLORER,
        limit=12,
    )

    assert packet.cards == []
    assert packet.retrieval_event_id.startswith("MEMORY-DISABLED:")
    with sqlite3.connect(runner.database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM memory_retrieval_events"
        ).fetchone()[0]
    assert count == 0


def test_exact_code_duplicate_skips_frozen_evaluator(tmp_path, contract, candidate) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    candidate_file = repo / "candidates" / "model.py"
    candidate_file.parent.mkdir(parents=True)
    candidate_file.write_text("VALUE = 'seed'\n", encoding="utf-8")
    prompts = repo / "prompts"
    prompts.mkdir()
    (prompts / "explorer.md").write_text("Explore safely.\n", encoding="utf-8")
    (prompts / "implementer.md").write_text("Implement safely.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "baseline",
    )
    base_commit = _git(repo, "rev-parse", "HEAD")
    contract = contract.model_copy(deep=True)
    contract.campaign.base_commit = base_commit
    contract.budgets = Budgets(implementations=2, mechanics_runs=2)
    contract.lock = ContractLock(
        content_sha256=sha256_object(
            contract.model_dump(mode="python", exclude={"lock"})
        )
    )

    class SameCodeBackend:
        def run(self, *, role, workdir: Path, output_path: Path, **kwargs):
            del kwargs
            assert role.name == "implementer"
            (workdir / "candidates" / "model.py").write_text(
                "VALUE = 'same-code'\n", encoding="utf-8"
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(
                    {
                        "status": "IMPLEMENTED",
                        "summary": "implemented exact duplicate",
                        "tests": [],
                    }
                ),
                encoding="utf-8",
            )
            return {"status": "PASS"}

    evaluator_calls = 0

    def evaluate(context: AutonomousEvaluationContext) -> EvaluationRun:
        nonlocal evaluator_calls
        evaluator_calls += 1
        current = context.candidate.acquisition.candidate
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=contract.lock.content_sha256,
                candidate=current,
                stage=context.candidate.stage,
                changed_files=["candidates/model.py"],
                mechanics_status=MechanicsStatus.PASS,
                data_eligible=True,
                metrics={
                    "clearance_mae_delta": -0.1,
                    "false_block_delta_pp": 0.0,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["fake-evaluator"],
            elapsed_seconds=0.01,
        )

    run_dir = tmp_path / "run"
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-DUPLICATE"),
        repo_root=repo,
        run_dir=run_dir,
        evaluate=evaluate,
        backend=SameCodeBackend(),
    )
    items = [
        CampaignCandidate(
            acquisition={
                "candidate": candidate.model_copy(
                    update={"candidate_id": candidate_id, "island": "main"}
                ),
                "signals": {
                    "admit_probability": 1.0,
                    "expected_improvement": 0.0,
                    "information_gain": 0.5,
                    "novelty": 0.5,
                },
            }
        )
        for candidate_id in ("DUPLICATE-CANDIDATE-001", "DUPLICATE-CANDIDATE-002")
    ]
    runner._implement_and_evaluate("GEN-001", items[0])
    with pytest.raises(DuplicateCandidateCodeError):
        runner._implement_and_evaluate("GEN-001", items[1])
    assert evaluator_calls == 1
    rejection = json.loads(
        (
            run_dir
            / "candidates"
            / "DUPLICATE-CANDIDATE-002"
            / "duplicate_code.json"
        ).read_text(encoding="utf-8")
    )
    assert rejection["duplicate_of"] == "DUPLICATE-CANDIDATE-001"
    assert rejection["evaluator_executed"] is False
