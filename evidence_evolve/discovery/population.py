from __future__ import annotations

import hashlib
import json
import sqlite3
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from evidence_evolve.models import (
    CandidateGenome,
    ScientificOutcome,
    SearchDisposition,
    StrictModel,
)


class PopulationRole(StrEnum):
    ELITE = "ELITE"
    NOVELTY = "NOVELTY"
    FAILURE = "FAILURE"
    STEPPING_STONE = "STEPPING_STONE"
    MIGRANT = "MIGRANT"


class MigrationEvent(StrictModel):
    generation_id: str
    candidate_id: str
    source_island: str
    target_island: str


class PopulationMember(StrictModel):
    candidate_id: str
    home_island: str
    island: str
    candidate_commit: str
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    behavior_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_disposition: SearchDisposition
    scientific_outcome: ScientificOutcome
    roles: list[PopulationRole]
    acquisition_score: float | None = None
    information_gain: float = 0.0
    novelty: float = 0.0
    admitted_generation: str
    active: bool


def behavior_key(candidate: CandidateGenome) -> str:
    """Hash an executable diversity descriptor, with a stable semantic fallback."""
    payload: dict[str, object]
    if candidate.behavior_descriptor:
        payload = {"behavior_descriptor": candidate.behavior_descriptor}
    else:
        payload = {
            "family": candidate.family,
            "mutation_type": candidate.mutation_type.value,
            "search_abstraction": candidate.search_abstraction.value,
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class DuplicateCandidateCodeError(RuntimeError):
    def __init__(self, candidate_id: str, duplicate_of: str, code_sha256: str):
        self.candidate_id = candidate_id
        self.duplicate_of = duplicate_of
        self.code_sha256 = code_sha256
        super().__init__(
            f"candidate {candidate_id} duplicates code artifact {duplicate_of} "
            f"({code_sha256})"
        )


class PopulationStore:
    """Persistent, bounded multi-island search state.

    This store grants search rights only. It never derives a scientific verdict or
    changes a claim ceiling.
    """

    def __init__(self, database: Path):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS code_artifacts (
                    code_sha256 TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE,
                    generation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS population_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    home_island TEXT NOT NULL,
                    candidate_commit TEXT NOT NULL,
                    code_sha256 TEXT NOT NULL UNIQUE,
                    behavior_key TEXT NOT NULL,
                    search_disposition TEXT NOT NULL,
                    scientific_outcome TEXT NOT NULL,
                    acquisition_score REAL,
                    information_gain REAL NOT NULL,
                    novelty REAL NOT NULL,
                    admitted_generation TEXT NOT NULL,
                    FOREIGN KEY(code_sha256) REFERENCES code_artifacts(code_sha256)
                );
                CREATE TABLE IF NOT EXISTS island_memberships (
                    island TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    source_island TEXT,
                    active INTEGER NOT NULL,
                    admitted_generation TEXT NOT NULL,
                    PRIMARY KEY(island, candidate_id),
                    FOREIGN KEY(candidate_id) REFERENCES population_candidates(candidate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_island_memberships_active
                    ON island_memberships(island, active, admitted_generation);
                CREATE TABLE IF NOT EXISTS island_migrations (
                    generation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    source_island TEXT NOT NULL,
                    target_island TEXT NOT NULL,
                    PRIMARY KEY(generation_id, candidate_id, target_island)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def claim_code(
        self, *, candidate_id: str, generation_id: str, code_sha256: str
    ) -> str | None:
        """Atomically claim a genotype hash, returning the prior owner if duplicate."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT candidate_id FROM code_artifacts WHERE code_sha256 = ?",
                (code_sha256,),
            ).fetchone()
            if owner is not None:
                return None if owner[0] == candidate_id else str(owner[0])
            prior = connection.execute(
                "SELECT code_sha256 FROM code_artifacts WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if prior is not None:
                if prior[0] != code_sha256:
                    raise ValueError(
                        f"candidate code changed after attribution: {candidate_id}"
                    )
                return None
            try:
                connection.execute(
                    "INSERT INTO code_artifacts(code_sha256, candidate_id, generation_id) "
                    "VALUES (?, ?, ?)",
                    (code_sha256, candidate_id, generation_id),
                )
            except sqlite3.IntegrityError:
                owner = connection.execute(
                    "SELECT candidate_id FROM code_artifacts WHERE code_sha256 = ?",
                    (code_sha256,),
                ).fetchone()
                if owner is None:
                    raise
                return None if owner[0] == candidate_id else str(owner[0])
        return None

    def admit(
        self,
        *,
        candidate: CandidateGenome,
        generation_id: str,
        candidate_commit: str,
        code_sha256: str,
        search_disposition: SearchDisposition,
        scientific_outcome: ScientificOutcome,
        acquisition_score: float | None,
        information_gain: float,
        novelty: float,
        parent_dispositions: set[SearchDisposition],
        stepping_stone_min_information_gain: float,
        island_capacity: int,
    ) -> PopulationMember | None:
        if search_disposition not in parent_dispositions:
            return None
        descriptor = behavior_key(candidate)
        with self._connect() as connection:
            descriptor_seen = connection.execute(
                """
                SELECT 1
                FROM population_candidates AS pc
                JOIN island_memberships AS im ON im.candidate_id = pc.candidate_id
                WHERE im.island = ? AND pc.behavior_key = ?
                LIMIT 1
                """,
                (candidate.island, descriptor),
            ).fetchone()
            roles: set[PopulationRole] = set()
            if search_disposition is SearchDisposition.CODE_PARENT:
                roles.add(PopulationRole.ELITE)
            if search_disposition is SearchDisposition.FAILURE_DIRECTED_SEED:
                roles.add(PopulationRole.FAILURE)
            if descriptor_seen is None:
                roles.add(PopulationRole.NOVELTY)
                if information_gain >= stepping_stone_min_information_gain:
                    roles.add(PopulationRole.STEPPING_STONE)
            roles_json = json.dumps(sorted(role.value for role in roles))
            values = (
                candidate.candidate_id,
                candidate.island,
                candidate_commit,
                code_sha256,
                descriptor,
                search_disposition.value,
                scientific_outcome.value,
                acquisition_score,
                information_gain,
                novelty,
                generation_id,
            )
            connection.execute(
                """
                INSERT INTO population_candidates(
                    candidate_id, home_island, candidate_commit, code_sha256,
                    behavior_key, search_disposition, scientific_outcome,
                    acquisition_score, information_gain, novelty, admitted_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO NOTHING
                """,
                values,
            )
            existing = connection.execute(
                """
                SELECT home_island, candidate_commit, code_sha256, behavior_key,
                       search_disposition, scientific_outcome, acquisition_score,
                       information_gain, novelty, admitted_generation
                FROM population_candidates WHERE candidate_id = ?
                """,
                (candidate.candidate_id,),
            ).fetchone()
            if existing != values[1:]:
                raise ValueError(
                    f"population candidate drift for {candidate.candidate_id}"
                )
            connection.execute(
                """
                INSERT INTO island_memberships(
                    island, candidate_id, roles_json, source_island, active,
                    admitted_generation
                ) VALUES (?, ?, ?, NULL, 1, ?)
                ON CONFLICT(island, candidate_id) DO NOTHING
                """,
                (candidate.island, candidate.candidate_id, roles_json, generation_id),
            )
        self._enforce_capacity(candidate.island, island_capacity)
        return self.member(candidate.island, candidate.candidate_id)

    def member(self, island: str, candidate_id: str) -> PopulationMember:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pc.candidate_id, pc.home_island, im.island,
                       pc.candidate_commit, pc.code_sha256, pc.behavior_key,
                       pc.search_disposition, pc.scientific_outcome, im.roles_json,
                       pc.acquisition_score, pc.information_gain, pc.novelty,
                       im.admitted_generation, im.active
                FROM island_memberships AS im
                JOIN population_candidates AS pc ON pc.candidate_id = im.candidate_id
                WHERE im.island = ? AND pc.candidate_id = ?
                """,
                (island, candidate_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"population member not found: {island}/{candidate_id}")
        return self._member_from_row(row)

    def parent_commits(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT candidate_id, candidate_commit FROM population_candidates"
            ).fetchall()
        return {str(candidate_id): str(commit) for candidate_id, commit in rows}

    def sample_parents(self, island: str, limit: int) -> list[PopulationMember]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT pc.candidate_id, pc.home_island, im.island,
                       pc.candidate_commit, pc.code_sha256, pc.behavior_key,
                       pc.search_disposition, pc.scientific_outcome, im.roles_json,
                       pc.acquisition_score, pc.information_gain, pc.novelty,
                       im.admitted_generation, im.active
                FROM island_memberships AS im
                JOIN population_candidates AS pc ON pc.candidate_id = im.candidate_id
                WHERE im.island = ? AND im.active = 1
                ORDER BY im.admitted_generation DESC, pc.candidate_id DESC
                """,
                (island,),
            ).fetchall()
        members = [self._member_from_row(row) for row in rows]
        selected: list[PopulationMember] = []
        for role in (
            PopulationRole.ELITE,
            PopulationRole.NOVELTY,
            PopulationRole.FAILURE,
            PopulationRole.STEPPING_STONE,
            PopulationRole.MIGRANT,
        ):
            candidates = sorted(
                (item for item in members if role in item.roles),
                key=self._selection_key,
                reverse=True,
            )
            if candidates and candidates[0] not in selected:
                selected.append(candidates[0])
                if len(selected) == limit:
                    return selected
        for item in sorted(members, key=self._selection_key, reverse=True):
            if item not in selected:
                selected.append(item)
                if len(selected) == limit:
                    break
        return selected

    def migrate(
        self,
        *,
        generation_id: str,
        generation_index: int,
        island_ids: list[str],
        migration_interval: int,
        migration_count: int,
        island_capacity: int,
    ) -> list[MigrationEvent]:
        if (
            len(island_ids) < 2
            or migration_count == 0
            or generation_index <= 1
            or (generation_index - 1) % migration_interval != 0
        ):
            return []
        with self._connect() as connection:
            existing_rows = connection.execute(
                """
                SELECT generation_id, candidate_id, source_island, target_island
                FROM island_migrations
                WHERE generation_id = ?
                ORDER BY source_island, target_island, candidate_id
                """,
                (generation_id,),
            ).fetchall()
        if existing_rows:
            return [
                MigrationEvent(
                    generation_id=str(row[0]),
                    candidate_id=str(row[1]),
                    source_island=str(row[2]),
                    target_island=str(row[3]),
                )
                for row in existing_rows
            ]
        events: list[MigrationEvent] = []
        migration_plan = {
            source: self.sample_parents(source, migration_count)
            for source in island_ids
        }
        for index, source in enumerate(island_ids):
            target = island_ids[(index + 1) % len(island_ids)]
            for member in migration_plan[source]:
                with self._connect() as connection:
                    existing = connection.execute(
                        "SELECT 1 FROM island_memberships WHERE island = ? AND candidate_id = ?",
                        (target, member.candidate_id),
                    ).fetchone()
                    if existing is not None:
                        continue
                    roles = sorted(
                        {*(role.value for role in member.roles), PopulationRole.MIGRANT.value}
                    )
                    connection.execute(
                        """
                        INSERT INTO island_memberships(
                            island, candidate_id, roles_json, source_island, active,
                            admitted_generation
                        ) VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (
                            target,
                            member.candidate_id,
                            json.dumps(roles),
                            source,
                            generation_id,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO island_migrations(
                            generation_id, candidate_id, source_island, target_island
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (generation_id, member.candidate_id, source, target),
                    )
                events.append(
                    MigrationEvent(
                        generation_id=generation_id,
                        candidate_id=member.candidate_id,
                        source_island=source,
                        target_island=target,
                    )
                )
                self._enforce_capacity(target, island_capacity)
        return events

    def snapshot(self) -> dict[str, list[dict[str, object]]]:
        with self._connect() as connection:
            islands = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT island FROM island_memberships ORDER BY island"
                ).fetchall()
            ]
        return {
            island: [
                item.model_dump(mode="json")
                for item in self._all_members(island, active_only=True)
            ]
            for island in islands
        }

    def _all_members(
        self, island: str, *, active_only: bool
    ) -> list[PopulationMember]:
        predicate = "AND im.active = 1" if active_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT pc.candidate_id, pc.home_island, im.island,
                       pc.candidate_commit, pc.code_sha256, pc.behavior_key,
                       pc.search_disposition, pc.scientific_outcome, im.roles_json,
                       pc.acquisition_score, pc.information_gain, pc.novelty,
                       im.admitted_generation, im.active
                FROM island_memberships AS im
                JOIN population_candidates AS pc ON pc.candidate_id = im.candidate_id
                WHERE im.island = ? {predicate}
                ORDER BY im.admitted_generation, pc.candidate_id
                """,
                (island,),
            ).fetchall()
        return [self._member_from_row(row) for row in rows]

    def _enforce_capacity(self, island: str, capacity: int) -> None:
        members = self._all_members(island, active_only=True)
        if len(members) <= capacity:
            return
        keep = {
            item.candidate_id
            for item in sorted(members, key=self._selection_key, reverse=True)[:capacity]
        }
        with self._connect() as connection:
            connection.executemany(
                "UPDATE island_memberships SET active = 0 "
                "WHERE island = ? AND candidate_id = ?",
                [
                    (island, item.candidate_id)
                    for item in members
                    if item.candidate_id not in keep
                ],
            )

    @staticmethod
    def _selection_key(member: PopulationMember) -> tuple[object, ...]:
        roles = set(member.roles)
        return (
            PopulationRole.STEPPING_STONE in roles,
            PopulationRole.ELITE in roles,
            PopulationRole.NOVELTY in roles,
            PopulationRole.FAILURE in roles,
            member.acquisition_score if member.acquisition_score is not None else float("-inf"),
            member.information_gain,
            member.novelty,
            member.admitted_generation,
            member.candidate_id,
        )

    @staticmethod
    def _member_from_row(row: tuple[object, ...]) -> PopulationMember:
        return PopulationMember(
            candidate_id=str(row[0]),
            home_island=str(row[1]),
            island=str(row[2]),
            candidate_commit=str(row[3]),
            code_sha256=str(row[4]),
            behavior_key=str(row[5]),
            search_disposition=SearchDisposition(str(row[6])),
            scientific_outcome=ScientificOutcome(str(row[7])),
            roles=[PopulationRole(item) for item in json.loads(str(row[8]))],
            acquisition_score=None if row[9] is None else float(row[9]),
            information_gain=float(row[10]),
            novelty=float(row[11]),
            admitted_generation=str(row[12]),
            active=bool(row[13]),
        )
