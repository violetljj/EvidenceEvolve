from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import Field

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.artifacts import atomic_write_json, create_once_json
from evidence_evolve.backends.codex_cli import CodexCliBackend, CodexRole
from evidence_evolve.budgets import BudgetLedger
from evidence_evolve.discovery.campaign import (
    CampaignCandidate,
    CampaignGenerationResult,
    CampaignRunner,
    EvaluationRun,
)
from evidence_evolve.governance.candidate_auditor import audit_candidate
from evidence_evolve.governance.closure_registry import ClosureRegistry
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_evolve.models import ResearchContract, ResearchStage, StrictModel
from evidence_evolve.worktrees import WorktreeManager


class CodexExecutionBackend(Protocol):
    def run(
        self,
        *,
        role: CodexRole,
        prompt: str,
        workdir: Path,
        output_schema: Path,
        output_path: Path,
        events_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> dict[str, object]: ...


class ImplementationManifest(StrictModel):
    status: Literal["IMPLEMENTED", "NO_CHANGE", "BLOCKED"]
    summary: str = Field(min_length=1)
    tests: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class AutonomousEvaluationContext:
    generation_id: str
    candidate: CampaignCandidate
    contract: ResearchContract
    repo_root: Path
    worktree: Path
    run_dir: Path


AutonomousEvaluationAdapter = Callable[[AutonomousEvaluationContext], EvaluationRun]


class AutonomousCampaignResult(StrictModel):
    campaign_id: str
    policy_id: str
    generations: list[CampaignGenerationResult]
    budgets: dict[str, dict[str, int]]


class AutonomousCampaignRunner:
    """Run a bounded proposal -> implementation -> evaluation feedback loop.

    Codex supplies creative proposals and candidate-only edits. The existing
    CampaignRunner remains the sole gate and receipt authority.
    """

    def __init__(
        self,
        *,
        contract: ResearchContract,
        closure_registry: ClosureRegistry,
        policy: ResearchPolicyGenome,
        repo_root: Path,
        run_dir: Path,
        evaluate: AutonomousEvaluationAdapter,
        backend: CodexExecutionBackend | None = None,
        worktree_root: Path | None = None,
        reference_metrics: dict[str, float] | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        if contract.lock is None:
            raise ValueError("autonomous campaign requires a locked research contract")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.contract = contract
        self.closure_registry = closure_registry
        self.policy = policy
        self.repo_root = repo_root.resolve()
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.evaluate = evaluate
        self.backend = backend or CodexCliBackend()
        self.worktrees = WorktreeManager(self.repo_root, worktree_root)
        self.reference_metrics = dict(reference_metrics or {})
        self.timeout_seconds = timeout_seconds
        self.database = self.run_dir / "research.db"
        self.budgets = BudgetLedger(self.database, contract.budgets)
        self.archive = ArchiveStore(self.database)
        self.campaign = CampaignRunner(
            contract=contract,
            closure_registry=closure_registry,
            policy=policy,
            run_dir=self.run_dir,
        )

    def run(
        self,
        *,
        generations: int,
        proposals_per_generation: int = 1,
        max_evaluations_per_generation: int | None = None,
        generation_prefix: str = "GEN",
        signature_tolerance: float = 0.0,
    ) -> AutonomousCampaignResult:
        if generations <= 0:
            raise ValueError("generations must be positive")
        if proposals_per_generation <= 0:
            raise ValueError("proposals_per_generation must be positive")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", generation_prefix):
            raise ValueError("generation_prefix is not a safe identifier")

        completed: list[CampaignGenerationResult] = []
        eligible_parents = ["SEED"]
        for generation_index in range(1, generations + 1):
            generation_id = f"{generation_prefix}-{generation_index:03d}"
            feedback = self._feedback_context(completed)
            candidates = [
                self._propose_candidate(
                    generation_id=generation_id,
                    slot=slot,
                    eligible_parents=eligible_parents,
                    feedback=feedback,
                )
                for slot in range(1, proposals_per_generation + 1)
            ]
            result = self.campaign.run_generation(
                generation_id=generation_id,
                candidates=candidates,
                evaluate=lambda item, generation_id=generation_id: (
                    self._implement_and_evaluate(generation_id, item)
                ),
                max_evaluations=max_evaluations_per_generation,
                signature_tolerance=signature_tolerance,
            )
            completed.append(result)
            evaluated_ids = [item.candidate_id for item in result.evaluations]
            if generation_index < generations:
                if not evaluated_ids:
                    raise RuntimeError(
                        f"generation {generation_id} produced no evaluated parent"
                    )
                eligible_parents = evaluated_ids

        return AutonomousCampaignResult(
            campaign_id=self.contract.campaign.id,
            policy_id=self.policy.policy_id,
            generations=completed,
            budgets=self.budgets.snapshot(),
        )

    def _propose_candidate(
        self,
        *,
        generation_id: str,
        slot: int,
        eligible_parents: list[str],
        feedback: dict[str, object],
    ) -> CampaignCandidate:
        candidate_id = f"{generation_id}-C{slot:02d}"
        generation_dir = self.run_dir / "generations" / generation_id
        proposal_path = generation_dir / "proposals" / f"{candidate_id}.json"
        if proposal_path.exists():
            candidate = CampaignCandidate.model_validate_json(
                proposal_path.read_text(encoding="utf-8")
            )
            self._validate_proposal_identity(candidate, candidate_id, eligible_parents)
            return candidate.model_copy(
                update={"reference_metrics": dict(self.reference_metrics)}
            )

        reservation_key = f"proposal_calls:{generation_id}:{candidate_id}"
        self.budgets.reserve("proposal_calls", 1, reservation_key)
        schema_path = generation_dir / "schemas" / f"{candidate_id}.schema.json"
        schema = self._proposal_schema(candidate_id, eligible_parents)
        atomic_write_json(schema_path, schema)

        raw_path = generation_dir / "raw" / f"{candidate_id}.json"
        events_path = generation_dir / "logs" / f"{candidate_id}.proposal.events.jsonl"
        stderr_path = generation_dir / "logs" / f"{candidate_id}.proposal.stderr.log"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = self._proposal_prompt(
            candidate_id=candidate_id,
            generation_id=generation_id,
            eligible_parents=eligible_parents,
            feedback=feedback,
        )
        result = self.backend.run(
            role=CodexRole("hypothesis_explorer"),
            prompt=prompt,
            workdir=self._proposal_workspace(),
            output_schema=schema_path,
            output_path=raw_path,
            events_path=events_path,
            stderr_path=stderr_path,
            timeout_seconds=self.timeout_seconds,
        )
        if result.get("status") != "PASS":
            raise RuntimeError(
                f"Codex proposal failed for {candidate_id}: {result.get('status')}"
            )
        if not raw_path.is_file():
            raise RuntimeError(f"Codex proposal output missing for {candidate_id}")
        candidate = CampaignCandidate.model_validate_json(
            raw_path.read_text(encoding="utf-8")
        )
        self._validate_proposal_identity(candidate, candidate_id, eligible_parents)
        if candidate.reference_metrics:
            raise ValueError("Codex proposals cannot supply reference metrics")
        declared = audit_candidate(
            self.contract,
            candidate.acquisition.candidate,
            ClosureRegistry(),
            changed_files=[],
        )
        declared_scope_violations = [
            issue for issue in declared.violations if issue.startswith("DECLARED_")
        ]
        if declared_scope_violations:
            raise ValueError(
                "Codex proposal declared invalid editable scope: "
                + ",".join(declared_scope_violations)
            )
        normalized = candidate.model_copy(
            update={"reference_metrics": dict(self.reference_metrics)}
        )
        create_once_json(proposal_path, normalized)
        raw_path.unlink(missing_ok=True)
        return normalized

    def _implement_and_evaluate(
        self, generation_id: str, item: CampaignCandidate
    ) -> EvaluationRun:
        candidate = item.acquisition.candidate
        candidate_dir = self.run_dir / "candidates" / candidate.candidate_id
        implementation_path = candidate_dir / "implementation.json"
        run_hash = hashlib.sha256(str(self.run_dir).encode("utf-8")).hexdigest()[:8]
        worktree_key = (
            f"{self.contract.campaign.id}-{run_hash}-{candidate.candidate_id}"
        )
        worktree = self.worktrees.candidate_path(worktree_key)
        if not worktree.exists():
            worktree = self.worktrees.create(
                worktree_key, self.contract.campaign.base_commit
            )
        self._assert_worktree_descends_from_base(worktree)

        if implementation_path.exists():
            manifest = ImplementationManifest.model_validate_json(
                implementation_path.read_text(encoding="utf-8")
            )
        else:
            self.budgets.reserve(
                "implementations",
                1,
                f"implementations:{generation_id}:{candidate.candidate_id}",
            )
            schema_path = candidate_dir / "implementation.schema.json"
            atomic_write_json(schema_path, ImplementationManifest.model_json_schema())
            raw_path = candidate_dir / "implementation.raw.json"
            result = self.backend.run(
                role=CodexRole("implementer", writable=True),
                prompt=self._implementation_prompt(item),
                workdir=worktree,
                output_schema=schema_path,
                output_path=raw_path,
                events_path=candidate_dir / "logs" / "implementation.events.jsonl",
                stderr_path=candidate_dir / "logs" / "implementation.stderr.log",
                timeout_seconds=self.timeout_seconds,
            )
            if result.get("status") != "PASS":
                raise RuntimeError(
                    "Codex implementation failed for "
                    f"{candidate.candidate_id}: {result.get('status')}"
                )
            if not raw_path.is_file():
                raise RuntimeError(
                    f"Codex implementation output missing for {candidate.candidate_id}"
                )
            manifest = ImplementationManifest.model_validate_json(
                raw_path.read_text(encoding="utf-8")
            )
            create_once_json(implementation_path, manifest)
            raw_path.unlink(missing_ok=True)

        if manifest.status == "BLOCKED":
            raise RuntimeError(
                f"Codex implementer blocked for {candidate.candidate_id}: "
                f"{manifest.summary}"
            )
        self._commit_valid_candidate_changes(worktree, item)
        return self.evaluate(
            AutonomousEvaluationContext(
                generation_id=generation_id,
                candidate=item,
                contract=self.contract,
                repo_root=self.repo_root,
                worktree=worktree,
                run_dir=self.run_dir,
            )
        )

    def _proposal_schema(
        self, candidate_id: str, eligible_parents: list[str]
    ) -> dict[str, object]:
        schema = copy.deepcopy(CampaignCandidate.model_json_schema())
        definitions = schema.get("$defs")
        if not isinstance(definitions, dict):
            raise RuntimeError("CampaignCandidate schema is missing definitions")
        genome = definitions.get("CandidateGenome")
        if not isinstance(genome, dict):
            raise RuntimeError("CampaignCandidate schema is missing CandidateGenome")
        properties = genome.get("properties")
        if not isinstance(properties, dict):
            raise RuntimeError("CandidateGenome schema is missing properties")
        properties["candidate_id"] = {
            "const": candidate_id,
            "title": "Candidate Id",
            "type": "string",
        }
        parent_ids = properties.get("parent_ids")
        if isinstance(parent_ids, dict):
            parent_ids["contains"] = {"enum": eligible_parents}
            parent_ids["minContains"] = 1
        root_properties = schema.get("properties")
        if isinstance(root_properties, dict):
            root_properties["stage"] = {
                "const": ResearchStage.M0_MECHANICS.value,
                "default": ResearchStage.M0_MECHANICS.value,
                "title": "Stage",
                "type": "string",
            }
            references = root_properties.get("reference_metrics")
            if isinstance(references, dict):
                references["maxProperties"] = 0
        acquisition = definitions.get("CandidateAcquisition")
        if isinstance(acquisition, dict):
            acquisition_properties = acquisition.get("properties")
            if isinstance(acquisition_properties, dict):
                verified = acquisition_properties.get("verified_reopen_conditions")
                if isinstance(verified, dict):
                    verified["maxItems"] = 0
        return schema

    @staticmethod
    def _validate_proposal_identity(
        candidate: CampaignCandidate,
        expected_id: str,
        eligible_parents: list[str],
    ) -> None:
        genome = candidate.acquisition.candidate
        if genome.candidate_id != expected_id:
            raise ValueError(
                f"proposal candidate id mismatch: expected={expected_id} "
                f"actual={genome.candidate_id}"
            )
        if not set(genome.parent_ids) & set(eligible_parents):
            raise ValueError(
                f"proposal {expected_id} does not descend from an evaluated parent"
            )
        if candidate.stage is not ResearchStage.M0_MECHANICS:
            raise ValueError("autonomous discovery proposals must start at M0_MECHANICS")
        if candidate.acquisition.verified_reopen_conditions:
            raise ValueError(
                "Codex proposals cannot supply verified reopen conditions"
            )

    def _proposal_prompt(
        self,
        *,
        candidate_id: str,
        generation_id: str,
        eligible_parents: list[str],
        feedback: dict[str, object],
    ) -> str:
        contract_context = {
            "campaign": self.contract.campaign.model_dump(mode="json"),
            "editable_scope": self.contract.editable_scope.model_dump(mode="json"),
            "metrics": self.contract.metrics.model_dump(mode="json"),
            "eligible_development_sources": [
                {
                    "source_id": source.source_id,
                    "grade": source.grade.value,
                    "permissions": sorted(item.value for item in source.permissions),
                }
                for source in self.contract.evidence_sources
                if any(item.value == "DEV" for item in source.permissions)
            ],
            "closures": self.closure_registry.model_dump(mode="json"),
        }
        return (
            self._prompt_template("explorer.md")
            + "\nYou are the EvidenceEvolve Hypothesis Explorer. Return exactly one "
            "CampaignCandidate matching the supplied JSON Schema. Your values are "
            "proposal and scheduling inputs only; do not claim scientific authority.\n"
            f"Required candidate_id: {candidate_id}\n"
            f"Generation: {generation_id}\n"
            f"Use at least one parent_id from: {json.dumps(eligible_parents)}\n"
            "Do not provide reference_metrics; the frozen harness supplies them. "
            "Do not access confirmation assets or claim reopen evidence. Propose one "
            "falsifiable mechanism change inside editable_scope.allow and list exact "
            "editable_files, controls, expected signatures, and a falsifier.\n"
            "Frozen context:\n"
            + json.dumps(contract_context, ensure_ascii=False, sort_keys=True)
            + "\nPrior evaluated feedback (SCHEDULING_ONLY where marked):\n"
            + json.dumps(feedback, ensure_ascii=False, sort_keys=True)
        )

    def _implementation_prompt(self, item: CampaignCandidate) -> str:
        return (
            self._prompt_template("implementer.md")
            + "\nYou are the EvidenceEvolve Implementer. Implement only the approved "
            "Candidate Genome in this dedicated Git worktree. Edit only files listed "
            "in candidate.editable_files and allowed by editable_scope. Never read or "
            "modify confirmation assets, evaluators, protocols, gates, budgets, or "
            "research policy. Do not commit. Run only narrow mechanics checks useful "
            "for the candidate. Return the required ImplementationManifest; make no "
            "scientific or verdict claim.\nCandidate:\n"
            + item.acquisition.candidate.model_dump_json(indent=2)
            + "\nEditable scope:\n"
            + self.contract.editable_scope.model_dump_json(indent=2)
        )

    def _prompt_template(self, name: str) -> str:
        path = self.repo_root / "prompts" / name
        if not path.is_file():
            raise FileNotFoundError(f"required Codex prompt is missing: {path}")
        return path.read_text(encoding="utf-8").strip()

    def _feedback_context(
        self, completed: list[CampaignGenerationResult]
    ) -> dict[str, object]:
        return {
            "archive_summary": self.archive.summary(),
            "completed_generations": [
                item.model_dump(mode="json") for item in completed
            ],
        }

    def _proposal_workspace(self) -> Path:
        digest = hashlib.sha256(str(self.run_dir).encode("utf-8")).hexdigest()[:16]
        workspace = (
            Path(tempfile.gettempdir())
            / "evidence-evolve-proposal-contexts"
            / digest
        )
        workspace.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
        return workspace

    def _assert_worktree_descends_from_base(self, worktree: Path) -> None:
        completed = subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                self.contract.campaign.base_commit,
                "HEAD",
            ],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"candidate worktree does not descend from frozen base: {worktree}"
            )

    def _commit_valid_candidate_changes(
        self, worktree: Path, item: CampaignCandidate
    ) -> None:
        changed_files = self.worktrees.changed_files(
            worktree, self.contract.campaign.base_commit
        )
        if not changed_files:
            return
        audit = audit_candidate(
            self.contract,
            item.acquisition.candidate,
            self.closure_registry,
            changed_files=changed_files,
            verified_reopen_conditions=item.acquisition.verified_reopen_conditions,
        )
        if not audit.valid:
            return
        subprocess.run(["git", "add", "--all"], cwd=worktree, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=worktree,
            check=False,
        )
        if staged.returncode == 0:
            return
        if staged.returncode != 1:
            raise RuntimeError("failed to inspect staged candidate changes")
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=EvidenceEvolve",
                "-c",
                "user.email=evidence-evolve@local.invalid",
                "commit",
                "-m",
                f"candidate: {item.acquisition.candidate.candidate_id}",
            ],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
