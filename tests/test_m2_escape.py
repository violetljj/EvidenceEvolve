from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from evidence_evolve.discovery.autonomous import AutonomousEvaluationContext
from evidence_evolve.discovery.campaign import EvaluationRun
from evidence_evolve.discovery.m2_escape import (
    M2AutonomousCampaignRunner,
    M2ControllerTrace,
    M2EscapeCampaignController,
    M2EscapePolicy,
)
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.hashing import sha256_object
from evidence_evolve.models import (
    Budgets,
    ContractLock,
    EvaluationInput,
    MechanicsStatus,
    MutationType,
    ScientificOutcome,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _FakeBackend:
    def __init__(self) -> None:
        self.prompts: list[str] = []
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
        self.prompts.append(prompt)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if role.name == "hypothesis_explorer":
            self.proposal_count += 1
            schema = json.loads(output_schema.read_text(encoding="utf-8"))
            properties = schema["$defs"]["CandidateGenome"]["properties"]
            candidate_id = properties["candidate_id"]["const"]
            parent = properties["genetic_parent_id"]["enum"][0]
            payload = {
                "acquisition": {
                    "candidate": {
                        "candidate_id": candidate_id,
                        "parent_ids": [parent],
                        "genetic_parent_id": parent,
                        "island": properties["island"]["const"],
                        "family": f"family-{self.proposal_count}",
                        "mutation_type": properties["mutation_type"]["const"],
                        "hypothesis": "A bounded structural change may improve the metric.",
                        "intervention": "Change the candidate implementation.",
                        "expected_signature": {
                            "improve": ["clearance_mae_delta"],
                            "unchanged": ["false_block_delta_pp"],
                        },
                        "falsifier": "The frozen development metric does not improve.",
                        "required_controls": ["wrong_factor", "zero_factor"],
                        "editable_files": ["candidates/model.py"],
                        "estimated_cost_tier": 1,
                    },
                    "signals": {
                        "admit_probability": 0.8,
                        "expected_improvement": 0.1,
                        "information_gain": 0.8,
                        "novelty": 0.8,
                    },
                }
            }
        else:
            candidate_id = re.search(r"GEN-\d{3}-C01", prompt).group(0)  # type: ignore[union-attr]
            (workdir / "candidates" / "model.py").write_text(
                f"VALUE = {candidate_id!r}\n", encoding="utf-8"
            )
            payload = {
                "status": "IMPLEMENTED",
                "summary": f"implemented {candidate_id}",
                "tests": ["fake focused check"],
            }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return {"status": "PASS", "command": ["fake-codex"]}


def test_m2_escape_uses_strict_incumbent_clock_and_protected_seed_roots(
    tmp_path: Path, contract
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
    contract = contract.model_copy(deep=True)
    contract.campaign.base_commit = _git(repo, "rev-parse", "HEAD")
    contract.budgets = Budgets(
        proposal_calls=5,
        implementations=5,
        mechanics_runs=5,
    )
    contract.lock = ContractLock(
        content_sha256=sha256_object(
            contract.model_dump(mode="python", exclude={"lock"})
        )
    )

    def evaluate(context: AutonomousEvaluationContext) -> EvaluationRun:
        candidate = context.candidate.acquisition.candidate
        metric = -0.1 if candidate.candidate_id == "GEN-001-C01" else 0.1
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=contract.lock.content_sha256,
                candidate=candidate,
                stage=context.candidate.stage,
                changed_files=["candidates/model.py"],
                mechanics_status=MechanicsStatus.PASS,
                data_eligible=True,
                metrics={
                    "clearance_mae_delta": metric,
                    "false_block_delta_pp": 0.0,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["fake-evaluator"],
            elapsed_seconds=0.01,
            candidate_commit=_git(context.worktree, "rev-parse", "HEAD"),
        )

    policy = M2EscapePolicy(
        policy_id="M2-TEST",
        stagnation_generations=3,
        incumbent_metric="clearance_mae_delta",
        escape_budget_generations=2,
        force_seed_restart_roots=True,
        island_capacity=1,
        moonshot_fraction=0.0,
        mutation_operator_mix={MutationType.MECHANISM: 1.0},
        breakthrough_mutation_mix={MutationType.RESTART: 1.0},
        literature_papers_per_action=0,
        repositories_per_action=0,
        source_files_per_repository=0,
    )
    backend = _FakeBackend()
    runner = M2AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=policy.frozen_base_policy(),
        repo_root=repo,
        run_dir=tmp_path / "run",
        evaluate=evaluate,
        backend=backend,
        reference_metrics={
            "clearance_mae_delta": 0.0,
            "false_block_delta_pp": 0.0,
        },
        memory_enabled=False,
    )
    result = M2EscapeCampaignController(runner=runner, policy=policy).run(
        generations=5
    )

    assert [trace.mode.value for trace in result.policy_effect_traces] == [
        "NORMAL",
        "NORMAL",
        "NORMAL",
        "NORMAL",
        "BREAKTHROUGH",
    ]
    third = M2ControllerTrace.model_validate_json(
        (
            runner.run_dir
            / "generations/GEN-003/m2_controller_state.json"
        ).read_text(encoding="utf-8")
    )
    assert third.parent_pool == ["GEN-001-C01"]
    assert "GEN-001-C01" not in third.admitted_parent_ids
    fifth = M2ControllerTrace.model_validate_json(
        (
            runner.run_dir
            / "generations/GEN-005/m2_controller_state.json"
        ).read_text(encoding="utf-8")
    )
    assert fifth.stagnant_generations_before == 3
    assert fifth.escape_triggered
    assert fifth.escape_budget_remaining_before == 2
    assert fifth.parent_pool == ["SEED"]
    proposal = json.loads(
        (
            runner.run_dir
            / "generations/GEN-005/proposals/GEN-005-C01.json"
        ).read_text(encoding="utf-8")
    )
    genome = proposal["acquisition"]["candidate"]
    assert genome["genetic_parent_id"] == "SEED"
    assert genome["mutation_type"] == "restart"
    assert any(
        "M2 BREAKTHROUGH is a protected structural operator class" in prompt
        for prompt in backend.prompts
    )
    manifest = json.loads(
        (runner.run_dir / "m2_controller_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["blind_artifacts_read"] is False
    assert manifest["scientific_authority"] == "NONE_SCHEDULING_ONLY"
