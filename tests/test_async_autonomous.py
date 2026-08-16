from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from evidence_evolve.discovery.async_autonomous import (
    AsyncAutonomousWaveRunner,
    AsyncWaveSlot,
    AsyncWaveSpec,
    MaterializedCandidate,
)
from evidence_evolve.discovery.autonomous import AutonomousCampaignRunner
from evidence_evolve.discovery.campaign import EvaluationRun
from evidence_evolve.discovery.throughput import (
    CandidateTicket,
    FunnelDecision,
    FunnelStage,
    StageStatus,
    ThroughputPolicy,
)
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.hashing import sha256_object
from evidence_evolve.meta_evolution.policy import DiscoveryMode, ResearchPolicyGenome
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


class _WaveBackend:
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
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if role.name == "hypothesis_explorer":
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
                        "family": f"family-{candidate_id}",
                        "mutation_type": properties["mutation_type"]["const"],
                        "hypothesis": "A deterministic wave candidate improves the metric.",
                        "intervention": "Write a candidate-specific implementation value.",
                        "expected_signature": {
                            "improve": ["clearance_mae_delta"],
                            "unchanged": ["false_block_delta_pp"],
                        },
                        "falsifier": "The full development metric fails to improve.",
                        "required_controls": ["wrong_factor", "zero_factor"],
                        "editable_files": ["candidates/model.py"],
                        "estimated_cost_tier": 1,
                    },
                    "signals": {
                        "admit_probability": 0.8,
                        "expected_improvement": 0.2,
                        "information_gain": 0.7,
                        "novelty": 0.7,
                    },
                }
            }
        else:
            candidate_id = re.search(r"WAVE-001-C\d{2}", prompt).group(0)  # type: ignore[union-attr]
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


class _WaveAdapter:
    def __init__(self, contract_sha256: str) -> None:
        self.contract_sha256 = contract_sha256

    @staticmethod
    def l0(
        ticket: CandidateTicket,
        item: MaterializedCandidate,
    ) -> FunnelDecision:
        assert ticket.candidate_id in (
            item.worktree / "candidates/model.py"
        ).read_text()
        return FunnelDecision(
            stage=FunnelStage.L0,
            status=StageStatus.PASS,
            continue_pipeline=True,
            mechanics_status=MechanicsStatus.PASS,
            data_eligible=False,
            scientific_outcome=ScientificOutcome.NOT_EVALUABLE_DATA,
            reason_codes=["MECHANICS_ONLY"],
        )

    @staticmethod
    def l1(
        ticket: CandidateTicket,
        item: MaterializedCandidate,
        l0: FunnelDecision,
    ) -> FunnelDecision:
        assert l0.continue_pipeline
        return FunnelDecision(
            stage=FunnelStage.L1,
            status=StageStatus.PASS,
            continue_pipeline=True,
            mechanics_status=MechanicsStatus.PASS,
            data_eligible=True,
            controls={"wrong_factor": True, "zero_factor": True},
            metrics={"clearance_mae_delta": -0.05, "false_block_delta_pp": 0.0},
            scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            reason_codes=["FROZEN_PROBE_PROMOTE"],
        )

    def full_evaluation(self, item: MaterializedCandidate) -> EvaluationRun:
        candidate = item.item.acquisition.candidate
        return EvaluationRun(
            evaluation=EvaluationInput(
                contract_sha256=self.contract_sha256,
                candidate=candidate,
                stage=item.item.stage,
                changed_files=item.changed_files,
                mechanics_status=MechanicsStatus.PASS,
                data_eligible=True,
                metrics={
                    "clearance_mae_delta": -0.1,
                    "false_block_delta_pp": 0.0,
                },
                controls={"wrong_factor": True, "zero_factor": True},
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            ),
            command=["fake-full-development"],
            elapsed_seconds=0.01,
            genetic_parent_id=item.genetic_parent_id,
            genetic_parent_commit=item.genetic_parent_commit,
            candidate_commit=item.candidate_commit,
            candidate_ref=item.candidate_ref,
            patch_sha256=item.patch_sha256,
            parent_patch_sha256=item.parent_patch_sha256,
        )

    @staticmethod
    def promotion_worthy(evaluation: EvaluationRun) -> bool:
        return evaluation.evaluation.metrics["clearance_mae_delta"] < 0.0

    @staticmethod
    def structural_transition_pass(
        ticket: CandidateTicket,
        item: MaterializedCandidate,
    ) -> bool:
        del item
        return ticket.requires_structural_transition

    @staticmethod
    def structural_root_key(
        ticket: CandidateTicket,
        item: MaterializedCandidate,
    ) -> str | None:
        del item
        return ticket.candidate_id if ticket.requires_structural_transition else None


def _run_wave(
    *,
    repo: Path,
    contract,
    run_dir: Path,
    worktree_root: Path,
    workers: int,
):
    runner = AutonomousCampaignRunner(
        contract=contract,
        closure_registry=ClosureRegistry(),
        policy=ResearchPolicyGenome(
            policy_id="ASYNC-WAVE-POLICY",
            moonshot_fraction=0.0,
            mutation_operator_mix={MutationType.MECHANISM: 1.0},
        ),
        repo_root=repo,
        run_dir=run_dir,
        evaluate=lambda _context: None,  # type: ignore[arg-type,return-value]
        backend=_WaveBackend(),
        worktree_root=worktree_root,
        reference_metrics={
            "clearance_mae_delta": 0.0,
            "false_block_delta_pp": 0.0,
        },
        memory_enabled=False,
    )
    wave = AsyncWaveSpec(
        wave_id="WAVE-001",
        slots=[
            AsyncWaveSlot(
                slot=1,
                dispatch_index=1,
                operator_class="local",
                lineage_id="LINEAGE-A",
                island="main",
                eligible_parent_ids=["SEED"],
                primary_parent_id="SEED",
                mutation=MutationType.MECHANISM,
                mode=DiscoveryMode.NORMAL,
            ),
            AsyncWaveSlot(
                slot=2,
                dispatch_index=2,
                operator_class="structural",
                lineage_id="LINEAGE-B",
                island="main",
                eligible_parent_ids=["SEED"],
                primary_parent_id="SEED",
                mutation=MutationType.MECHANISM,
                mode=DiscoveryMode.NORMAL,
                requires_structural_transition=True,
            ),
        ],
    )
    result = AsyncAutonomousWaveRunner(
        runner=runner,
        throughput_policy=ThroughputPolicy(
            policy_id=f"ASYNC-{workers}",
            total_candidate_budget=2,
            propose_workers=workers,
            implement_workers=workers,
            l0_workers=workers,
            l1_workers=workers,
            l2_workers=workers,
            max_inflight_per_lineage=1,
            operator_quotas={"local": 1, "structural": 1},
        ),
        staged_adapter=_WaveAdapter(contract.lock.content_sha256),
    ).run_wave(wave=wave, feedback={"scope": "FROZEN_TEST_CONTEXT"})
    return runner, result


def test_real_autonomous_wave_has_serial_async_receipt_parity(
    tmp_path: Path, contract
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    source = repo / "candidates/model.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 'seed'\n", encoding="utf-8")
    prompts = repo / "prompts"
    prompts.mkdir()
    (prompts / "explorer.md").write_text("Explore safely.\n")
    (prompts / "implementer.md").write_text("Implement safely.\n")
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
        proposal_calls=2,
        implementations=2,
        mechanics_runs=2,
    )
    contract.lock = ContractLock(
        content_sha256=sha256_object(
            contract.model_dump(mode="python", exclude={"lock"})
        )
    )

    serial_runner, serial = _run_wave(
        repo=repo,
        contract=contract,
        run_dir=tmp_path / "serial",
        worktree_root=tmp_path / "serial-worktrees",
        workers=1,
    )
    async_runner, parallel = _run_wave(
        repo=repo,
        contract=contract,
        run_dir=tmp_path / "parallel",
        worktree_root=tmp_path / "parallel-worktrees",
        workers=2,
    )

    assert serial.throughput.metrics.l2_completed == 2
    assert parallel.throughput.metrics.l2_completed == 2
    assert parallel.throughput.metrics.l2_dev_valid == 2
    assert parallel.throughput.metrics.structural_roots == 1
    assert parallel.throughput.metrics.incumbent_improvements == 2
    assert set(parallel.receipt_paths) == {"WAVE-001-C01", "WAVE-001-C02"}
    assert len(async_runner.population.snapshot()["main"]) == 2

    def receipts(runner, result) -> dict[str, dict[str, object]]:
        observed = {}
        for candidate_id, relative in result.receipt_paths.items():
            receipt = json.loads((runner.run_dir / relative).read_text())["receipt"]
            observed[candidate_id] = {
                "patch_sha256": receipt["patch_sha256"],
                "evaluation_input": receipt["evaluation_input"],
                "verdict": receipt["verdict"],
            }
        return observed

    assert receipts(serial_runner, serial) == receipts(async_runner, parallel)
