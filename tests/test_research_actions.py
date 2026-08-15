from __future__ import annotations

import base64
import json

from evidence_evolve.archive import ArchiveStore
from evidence_evolve.budgets import BudgetLedger
from evidence_evolve.discovery.director import ResearchAction, ResearchDirector
from evidence_evolve.meta_evolution.policy import ResearchPolicyGenome
from evidence_evolve.models import Budgets
from evidence_evolve.research_actions.intelligence import (
    LiteratureRepoIntelligenceExecutor,
)
from evidence_evolve.research_actions.models import (
    ActionState,
    ResearchActionJob,
)
from evidence_evolve.research_actions.store import ResearchActionRunner
from evidence_evolve.research_memory import MemoryKind, MemoryRole


class FakeIntelligenceTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str]) -> bytes:
        self.calls.append(url)
        assert "test-openalex-key" not in headers.values()
        if url.startswith("https://api.openalex.org/works?"):
            assert "api_key=test-openalex-key" in url
            return json.dumps(
                {
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "doi": "https://doi.org/10.1/example",
                            "display_name": "Support-aware residual geometry",
                            "publication_date": "2025-01-02",
                            "type": "article",
                            "cited_by_count": 3,
                            "authorships": [
                                {"author": {"display_name": "A. Researcher"}}
                            ],
                            "abstract_inverted_index": {
                                "Support": [0],
                                "reduces": [1],
                                "false-block": [2],
                            },
                            "open_access": {"is_oa": True},
                            "best_oa_location": {
                                "landing_page_url": "https://example.org/paper"
                            },
                            "topics": [{"display_name": "Depth estimation"}],
                        }
                    ]
                }
            ).encode()
        if url.startswith("https://api.github.com/search/repositories?"):
            return json.dumps(
                {
                    "items": [
                        {
                            "full_name": "example/support-depth",
                            "default_branch": "main",
                            "html_url": "https://github.com/example/support-depth",
                            "description": "Support-aware depth implementation",
                            "language": "Python",
                            "license": {"spdx_id": "Apache-2.0"},
                        }
                    ]
                }
            ).encode()
        if "/commits/main" in url:
            return json.dumps(
                {
                    "sha": "a" * 40,
                    "commit": {"tree": {"sha": "b" * 40}},
                }
            ).encode()
        if "/git/trees/" in url:
            return json.dumps(
                {
                    "sha": "b" * 40,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/support_model.py",
                            "type": "blob",
                            "size": 100,
                            "sha": "c" * 40,
                            "url": "https://api.github.com/repos/example/support-depth/git/blobs/"
                            + "c" * 40,
                        }
                    ],
                }
            ).encode()
        if "/git/blobs/" in url:
            return json.dumps(
                {
                    "encoding": "base64",
                    "content": base64.b64encode(
                        b"def support_residual(depth, support): return depth * support"
                    ).decode(),
                }
            ).encode()
        raise AssertionError(f"unexpected URL: {url}")


def _runner(tmp_path):
    database = tmp_path / "run" / "research.db"
    archive = ArchiveStore(database)
    budgets = BudgetLedger(
        database,
        Budgets(literature_searches=1, repository_inspections=1),
    )
    return archive, ResearchActionRunner(
        database=database,
        run_dir=tmp_path / "run",
        budgets=budgets,
    ), budgets


def test_literature_repo_action_executes_snapshots_and_changes_director_context(
    tmp_path,
) -> None:
    archive, runner, budgets = _runner(tmp_path)
    transport = FakeIntelligenceTransport()
    executor = LiteratureRepoIntelligenceExecutor(
        run_dir=tmp_path / "run",
        openalex_api_key="test-openalex-key",
        transport=transport,
    )
    job = ResearchActionJob(
        action_id="GEN-001-SEARCH-LITERATURE",
        campaign_id="campaign-a",
        generation_id="GEN-001",
        action=ResearchAction.SEARCH_LITERATURE,
        query="support aware depth false block",
        max_papers=1,
        max_repositories=1,
        max_source_files_per_repository=1,
    )
    result = runner.run(job, executor)
    assert result.state is ActionState.SUCCEEDED
    assert result.receipt is not None
    assert len(result.receipt.receipt.records) == 2
    assert all(
        "test-openalex-key" not in artifact.source_url
        for artifact in result.receipt.receipt.artifacts
    )
    assert budgets.snapshot()["literature_searches"]["used"] == 1
    assert budgets.snapshot()["repository_inspections"]["used"] == 1

    packet = archive.research_memory_packet(
        role=MemoryRole.RESEARCH_DIRECTOR,
        campaign="campaign-a",
        query="support depth",
        limit=12,
    )
    assert {card.kind for card in packet.cards} == {
        MemoryKind.MECHANISM,
        MemoryKind.PROCEDURE,
        MemoryKind.TRANSFER,
    }
    assert all(card.epistemics.evidence_basis == "EXTERNAL_SOURCE" for card in packet.cards)
    assert all(card.epistemics.scientific_outcome is None for card in packet.cards)
    assert all(card.provenance.action_receipt_ids for card in packet.cards)
    procedure = next(card for card in packet.cards if card.kind is MemoryKind.PROCEDURE)
    transfer = next(card for card in packet.cards if card.kind is MemoryKind.TRANSFER)
    assert procedure.content.mechanism_claims == []
    assert "support_residual" in transfer.content.mechanism_claims[0]
    assert len(transfer.content.mechanism_claims[0]) <= 2400

    policy = ResearchPolicyGenome(policy_id="POLICY-A")
    decision = ResearchDirector().decide(
        generation_id="GEN-001",
        packet=packet,
        stagnant_generations=0,
        stagnation_threshold=policy.stagnation_generations,
        default_mix=policy.mutation_operator_mix,
        breakthrough_mix=policy.breakthrough_mutation_mix,
    )
    assert decision.primary_action is ResearchAction.MUTATE
    calls = len(transport.calls)
    resumed = runner.run(job, executor)
    assert resumed.receipt == result.receipt
    assert len(transport.calls) == calls


def test_missing_openalex_authority_waits_without_consuming_budget(tmp_path) -> None:
    _, runner, budgets = _runner(tmp_path)
    result = runner.run(
        ResearchActionJob(
            action_id="WAITING-SEARCH",
            campaign_id="campaign-a",
            action=ResearchAction.SEARCH_LITERATURE,
            query="depth",
            max_papers=1,
            max_repositories=0,
        ),
        LiteratureRepoIntelligenceExecutor(
            run_dir=tmp_path / "run",
            openalex_api_key=None,
            transport=FakeIntelligenceTransport(),
        ),
    )
    assert result.state is ActionState.WAITING_FOR_AUTHORITY
    assert result.receipt is None
    assert budgets.snapshot()["literature_searches"]["used"] == 0
