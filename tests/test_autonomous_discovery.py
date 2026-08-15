from __future__ import annotations

import json
import subprocess
from pathlib import Path

from evidence_evolve.discovery.autonomous import (
    AutonomousCampaignRunner,
    AutonomousEvaluationContext,
)
from evidence_evolve.discovery.campaign import CampaignCandidate, EvaluationRun
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.hashing import sha256_object
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_evolve.models import (
    Budgets,
    ContractLock,
    EvaluationInput,
    MechanicsStatus,
    ScientificOutcome,
)


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
            parent = "SEED" if self.proposal_count == 1 else "GEN-001-C01"
            payload = {
                "acquisition": {
                    "candidate": {
                        "candidate_id": candidate_id,
                        "parent_ids": [parent],
                        "island": "test",
                        "family": f"family-{self.proposal_count}",
                        "mutation_type": "mechanism_mutation",
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
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["fake-evaluator"],
            elapsed_seconds=0.01,
            candidate_commit=_git(context.worktree, "rev-parse", "HEAD"),
        )

    backend = FakeCodexBackend()
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-AUTO"),
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
    assert len(result.generations) == 2
    assert result.generations[1].evaluations[0].candidate_id == "GEN-002-C01"
    second_proposal_prompt = [
        call["prompt"]
        for call in backend.calls
        if call["role"] == "hypothesis_explorer"
    ][1]
    assert "GEN-001-C01" in second_proposal_prompt
    assert "ADMIT" in second_proposal_prompt
    assert result.budgets["proposal_calls"]["used"] == 2
    assert result.budgets["implementations"]["used"] == 2
    assert result.budgets["mechanics_runs"]["used"] == 2

    resume_backend = FakeCodexBackend()
    resumed = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(policy_id="POLICY-AUTO"),
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
    schema = runner._proposal_schema("GEN-009-C03", ["PARENT-1"])
    genome = schema["$defs"]["CandidateGenome"]
    assert genome["properties"]["candidate_id"]["const"] == "GEN-009-C03"
    assert genome["properties"]["parent_ids"]["contains"] == {
        "enum": ["PARENT-1"]
    }
    assert schema["properties"]["reference_metrics"]["maxProperties"] == 0
    assert schema["properties"]["stage"]["const"] == "M0_MECHANICS"
    acquisition = schema["$defs"]["CandidateAcquisition"]
    assert acquisition["properties"]["verified_reopen_conditions"]["maxItems"] == 0
