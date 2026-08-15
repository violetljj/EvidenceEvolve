from __future__ import annotations

from evidence_evolve.discovery.population import PopulationRole, PopulationStore
from evidence_evolve.models import ScientificOutcome, SearchDisposition


def test_population_persists_stratified_roles_deduplicates_and_migrates(
    tmp_path, candidate
) -> None:
    database = tmp_path / "research.db"
    store = PopulationStore(database)
    elite = candidate.model_copy(
        update={
            "candidate_id": "ALPHA-ELITE-001",
            "island": "alpha",
            "behavior_descriptor": {"representation": "factorized"},
        }
    )
    failure = candidate.model_copy(
        update={
            "candidate_id": "BETA-FAILURE-001",
            "island": "beta",
            "behavior_descriptor": {"failure": "shape-sensitive"},
        }
    )
    elite_hash = "1" * 64
    failure_hash = "2" * 64
    assert store.claim_code(
        candidate_id=elite.candidate_id,
        generation_id="GEN-001",
        code_sha256=elite_hash,
    ) is None
    elite_member = store.admit(
        candidate=elite,
        generation_id="GEN-001",
        candidate_commit="a" * 40,
        code_sha256=elite_hash,
        search_disposition=SearchDisposition.CODE_PARENT,
        scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
        acquisition_score=1.2,
        information_gain=0.8,
        novelty=0.9,
        parent_dispositions={
            SearchDisposition.CODE_PARENT,
            SearchDisposition.FAILURE_DIRECTED_SEED,
        },
        stepping_stone_min_information_gain=0.6,
        island_capacity=4,
    )
    assert elite_member is not None
    assert set(elite_member.roles) == {
        PopulationRole.ELITE,
        PopulationRole.NOVELTY,
        PopulationRole.STEPPING_STONE,
    }

    assert store.claim_code(
        candidate_id=failure.candidate_id,
        generation_id="GEN-001",
        code_sha256=failure_hash,
    ) is None
    failure_member = store.admit(
        candidate=failure,
        generation_id="GEN-001",
        candidate_commit="b" * 40,
        code_sha256=failure_hash,
        search_disposition=SearchDisposition.FAILURE_DIRECTED_SEED,
        scientific_outcome=ScientificOutcome.VALID_NEGATIVE,
        acquisition_score=0.4,
        information_gain=0.9,
        novelty=0.8,
        parent_dispositions={
            SearchDisposition.CODE_PARENT,
            SearchDisposition.FAILURE_DIRECTED_SEED,
        },
        stepping_stone_min_information_gain=0.6,
        island_capacity=4,
    )
    assert failure_member is not None
    assert PopulationRole.FAILURE in failure_member.roles
    assert PopulationRole.STEPPING_STONE in failure_member.roles

    assert (
        store.claim_code(
            candidate_id="ALPHA-DUPLICATE-002",
            generation_id="GEN-002",
            code_sha256=elite_hash,
        )
        == elite.candidate_id
    )
    migrations = store.migrate(
        generation_id="GEN-002",
        generation_index=2,
        island_ids=["alpha", "beta"],
        migration_interval=1,
        migration_count=1,
        island_capacity=4,
    )
    assert {
        (item.candidate_id, item.source_island, item.target_island)
        for item in migrations
    } == {
        (elite.candidate_id, "alpha", "beta"),
        (failure.candidate_id, "beta", "alpha"),
    }
    assert store.migrate(
        generation_id="GEN-002",
        generation_index=2,
        island_ids=["alpha", "beta"],
        migration_interval=1,
        migration_count=1,
        island_capacity=4,
    ) == migrations
    assert PopulationRole.MIGRANT in store.member(
        "alpha", failure.candidate_id
    ).roles
    assert PopulationRole.MIGRANT in store.member("beta", elite.candidate_id).roles

    reopened = PopulationStore(database)
    assert reopened.parent_commits() == {
        elite.candidate_id: "a" * 40,
        failure.candidate_id: "b" * 40,
    }
    assert set(reopened.snapshot()) == {"alpha", "beta"}


def test_population_capacity_keeps_explicit_stepping_stone(tmp_path, candidate) -> None:
    store = PopulationStore(tmp_path / "research.db")
    for index, information_gain in enumerate((0.9, 0.1), start=1):
        item = candidate.model_copy(
            update={
                "candidate_id": f"CAPACITY-CANDIDATE-{index:03d}",
                "island": "bounded",
                "behavior_descriptor": {"route": str(index)},
            }
        )
        code_hash = str(index) * 64
        store.claim_code(
            candidate_id=item.candidate_id,
            generation_id=f"GEN-00{index}",
            code_sha256=code_hash,
        )
        store.admit(
            candidate=item,
            generation_id=f"GEN-00{index}",
            candidate_commit=str(index + 2) * 40,
            code_sha256=code_hash,
            search_disposition=SearchDisposition.CODE_PARENT,
            scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
            acquisition_score=float(index),
            information_gain=information_gain,
            novelty=0.5,
            parent_dispositions={SearchDisposition.CODE_PARENT},
            stepping_stone_min_information_gain=0.6,
            island_capacity=1,
        )
    active = store.sample_parents("bounded", 2)
    assert [item.candidate_id for item in active] == ["CAPACITY-CANDIDATE-001"]
    assert PopulationRole.STEPPING_STONE in active[0].roles
