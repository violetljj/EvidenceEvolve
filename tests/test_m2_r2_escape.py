from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from evidence_evolve.discovery.autonomous import AutonomousEvaluationContext
from evidence_evolve.discovery.campaign import EvaluationRun
from evidence_evolve.discovery.m2_r2_escape import (
    M2R2AutonomousCampaignRunner,
    M2R2EscapeCampaignController,
    M2R2FailureModel,
    M2R2Policy,
    StructuralEscapePlan,
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


class _R2Backend:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.proposal_count = 0
        self.plan_count = 0

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
        schema = json.loads(output_schema.read_text(encoding="utf-8"))
        if role.name == "hypothesis_explorer" and "mechanism_to_replace" in schema.get(
            "properties", {}
        ):
            self.plan_count += 1
            properties = schema["properties"]
            candidate_id = properties["candidate_id"]["const"]
            parent = properties["genetic_parent_id"]["enum"][0]
            context_id = properties["context_candidate_ids"]["items"]["enum"][0]
            payload = {
                "candidate_id": candidate_id,
                "operator_class": properties["operator_class"]["const"],
                "operator_directive": properties["operator_directive"]["const"],
                "genetic_parent_id": parent,
                "context_candidate_ids": [context_id],
                "addressed_failure_candidate_ids": [context_id],
                "preserved_mechanisms": ["deterministic interface normalization"],
                "mechanism_to_replace": "bounded greedy expansion",
                "replacement_mechanism": "primal dual relaxation",
                "target_family": "failure_directed_primal_dual_hybrid",
                "representation_change": "represent uncovered incidence as dual events",
                "solver_process_change": "replace greedy expansion with dual ascent",
                "integration_steps": ["retain normalization and replace the core solver"],
                "correctness_invariants": ["return only original valid indices"],
                "predicted_failure_mode": "dual bookkeeping overhead dominates",
                "falsifier": "full development is invalid or no faster",
            }
        elif role.name == "hypothesis_explorer":
            self.proposal_count += 1
            definitions = schema["$defs"]
            properties = definitions["CandidateGenome"]["properties"]
            candidate_id = properties["candidate_id"]["const"]
            parent = properties["genetic_parent_id"]["enum"][0]
            breakthrough = "M2-R2 requires a context-preserving" in prompt
            family = (
                "failure_directed_primal_dual_hybrid"
                if breakthrough
                else f"normal-family-{self.proposal_count}"
            )
            payload = {
                "acquisition": {
                    "candidate": {
                        "candidate_id": candidate_id,
                        "parent_ids": [parent],
                        "genetic_parent_id": parent,
                        "island": properties["island"]["const"],
                        "family": family,
                        "mutation_type": properties["mutation_type"]["const"],
                        "hypothesis": "A concrete mechanism substitution may improve speed.",
                        "intervention": "Replace the inherited core solver mechanism.",
                        "mechanism_claims": ["The solver process changes structurally."],
                        "expected_signature": {
                            "improve": ["clearance_mae_delta"],
                            "unchanged": ["false_block_delta_pp"],
                        },
                        "falsifier": "The frozen development metric does not improve.",
                        "required_controls": ["wrong_factor", "zero_factor"],
                        "ablation_plan": ["restore the inherited solver"],
                        "transfer_motifs": ["deterministic interface normalization"],
                        "failure_risks": ["bookkeeping overhead"],
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


def test_m2_r2_compiles_failure_model_and_mechanism_plan(
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
        proposal_calls=4,
        implementations=3,
        mechanics_runs=3,
    )
    contract.lock = ContractLock(
        content_sha256=sha256_object(
            contract.model_dump(mode="python", exclude={"lock"})
        )
    )

    def evaluate(context: AutonomousEvaluationContext) -> EvaluationRun:
        candidate = context.candidate.acquisition.candidate
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=contract.lock.content_sha256,
                candidate=candidate,
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
            candidate_commit=_git(context.worktree, "rev-parse", "HEAD"),
        )

    policy = M2R2Policy(
        policy_id="M2-R2-TEST",
        stagnation_generations=1,
        incumbent_metric="clearance_mae_delta",
        escape_budget_generations=1,
        island_capacity=4,
        parents_per_island=2,
        moonshot_fraction=0.0,
        mutation_operator_mix={MutationType.MECHANISM: 1.0},
        breakthrough_mutation_mix={
            MutationType.CROSS_FAMILY: 0.5,
            MutationType.FAILURE_DIRECTED: 0.5,
        },
        literature_papers_per_action=0,
        repositories_per_action=0,
        source_files_per_repository=0,
    )
    backend = _R2Backend()
    runner = M2R2AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=policy.frozen_base_policy(),
        r2_policy=policy,
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
    result = M2R2EscapeCampaignController(runner=runner, policy=policy).run(
        generations=3
    )

    assert [trace.mode.value for trace in result.policy_effect_traces] == [
        "NORMAL",
        "NORMAL",
        "BREAKTHROUGH",
    ]
    assert backend.plan_count == 1
    assert backend.proposal_count == 3
    operator_dir = runner.run_dir / "generations/GEN-003/r2_operator"
    failure_model = M2R2FailureModel.model_validate_json(
        (operator_dir / "GEN-003-C01.failure_model.json").read_text()
    )
    plan = StructuralEscapePlan.model_validate_json(
        (operator_dir / "GEN-003-C01.escape_plan.json").read_text()
    )
    assert failure_model.blind_artifacts_read is False
    assert failure_model.confirmation_artifacts_read is False
    assert set(failure_model.source_candidate_ids) == {"GEN-001-C01", "GEN-002-C01"}
    assert all(item.metrics["clearance_mae_delta"] == -0.1 for item in failure_model.observations)
    assert plan.genetic_parent_id != "SEED"
    assert plan.addressed_failure_candidate_ids[0] in failure_model.source_candidate_ids
    proposal = json.loads(
        (
            runner.run_dir
            / "generations/GEN-003/proposals/GEN-003-C01.json"
        ).read_text()
    )["acquisition"]["candidate"]
    assert proposal["genetic_parent_id"] == plan.genetic_parent_id
    assert proposal["family"] == plan.target_family
    assert result.budgets["proposal_calls"]["used"] == 4
    manifest = json.loads(
        (runner.run_dir / "m2_r2_controller_manifest.json").read_text()
    )
    assert manifest["operator_pipeline"][0] == "DETERMINISTIC_FAILURE_MODEL"
    assert manifest["blind_artifacts_read"] is False


def test_m2_r2_rejects_restart_allocation() -> None:
    with pytest.raises(ValueError, match="cannot allocate restart"):
        M2R2Policy(
            policy_id="BAD-R2",
            incumbent_metric="raw_speedup",
            breakthrough_mutation_mix={
                MutationType.CROSS_FAMILY: 0.5,
                MutationType.RESTART: 0.5,
            },
        )
