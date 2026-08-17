from __future__ import annotations

import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from evidence_evolve.models import StrictModel


class SmokeCase(StrictModel):
    task_id: str
    instance_id: str
    role: Literal["REGRESSION"]
    instance: dict[str, Any]
    solution: dict[str, Any]


class Core12SmokeInventory(StrictModel):
    schema_version: Literal["1.0"]
    authority: Literal["PUBLIC_REGRESSION_SMOKE_ONLY"]
    cases: list[SmokeCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> "Core12SmokeInventory":
        task_ids = [case.task_id for case in self.cases]
        instance_ids = [case.instance_id for case in self.cases]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("smoke inventory requires exactly one case per family")
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("smoke instance ids must be unique")
        return self


def load_smoke_inventory(path: Path) -> Core12SmokeInventory:
    with path.open("r", encoding="utf-8") as stream:
        return Core12SmokeInventory.model_validate(yaml.safe_load(stream) or {})


def _assert_indices(values: list[int], size: int) -> None:
    assert len(values) == len(set(values))
    assert all(0 <= value < size for value in values)


def _is_acyclic(vertices: set[int], arcs: list[list[int]]) -> bool:
    outgoing: dict[int, list[int]] = defaultdict(list)
    indegree = {vertex: 0 for vertex in vertices}
    for left, right in arcs:
        if left in vertices and right in vertices:
            outgoing[left].append(right)
            indegree[right] += 1
    queue = deque(vertex for vertex, degree in indegree.items() if degree == 0)
    seen = 0
    while queue:
        left = queue.popleft()
        seen += 1
        for right in outgoing[left]:
            indegree[right] -= 1
            if indegree[right] == 0:
                queue.append(right)
    return seen == len(vertices)


def _validate_assignment(case: SmokeCase) -> None:
    costs = case.instance["costs"]
    assignment = case.solution["assignment"]
    assert len(costs) == len(assignment)
    _assert_indices(assignment, len(costs))
    assert sum(costs[row][column] for row, column in enumerate(assignment)) == case.solution["objective"]


def _validate_knapsack(case: SmokeCase) -> None:
    selected = case.solution["selected"]
    weights = case.instance["weights"]
    values = case.instance["values"]
    _assert_indices(selected, len(weights))
    assert sum(weights[index] for index in selected) <= case.instance["capacity"]
    assert sum(values[index] for index in selected) == case.solution["objective"]


def _validate_set_cover(case: SmokeCase) -> None:
    sets = case.instance["sets"]
    selected = case.solution["selected"]
    _assert_indices(selected, len(sets))
    covered = {item for index in selected for item in sets[index]}
    assert covered == set(case.instance["universe"])
    assert len(selected) == case.solution["objective"]


def _validate_graph_coloring(case: SmokeCase) -> None:
    colors = case.solution["colors"]
    assert len(colors) == case.instance["vertex_count"]
    assert all(colors[left] != colors[right] for left, right in case.instance["edges"])
    assert len(set(colors)) == case.solution["objective"]


def _validate_steiner_tree(case: SmokeCase) -> None:
    weighted = {frozenset((left, right)): weight for left, right, weight in case.instance["edges"]}
    selected = case.solution["selected_edges"]
    adjacency: dict[int, set[int]] = defaultdict(set)
    for left, right in selected:
        assert frozenset((left, right)) in weighted
        adjacency[left].add(right)
        adjacency[right].add(left)
    reached = set()
    queue = deque([case.instance["terminals"][0]])
    while queue:
        vertex = queue.popleft()
        if vertex not in reached:
            reached.add(vertex)
            queue.extend(adjacency[vertex] - reached)
    assert set(case.instance["terminals"]) <= reached
    assert sum(weighted[frozenset(edge)] for edge in selected) == case.solution["objective"]


def _validate_cvrp(case: SmokeCase) -> None:
    depot = case.instance["depot"]
    demands = case.instance["demands"]
    coordinates = case.instance["coordinates"]
    visited: list[int] = []
    distance = 0.0
    for route in case.solution["routes"]:
        assert route[0] == depot and route[-1] == depot
        customers = route[1:-1]
        assert sum(demands[index] for index in customers) <= case.instance["capacity"]
        visited.extend(customers)
        for left, right in zip(route, route[1:]):
            distance += math.dist(coordinates[left], coordinates[right])
    assert sorted(visited) == list(range(1, len(demands)))
    assert distance == case.solution["objective"]


def _validate_flexible_job_shop(case: SmokeCase) -> None:
    operations = case.solution["operations"]
    by_job: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    by_machine: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for job, operation, machine, start, duration in operations:
        eligible = dict(case.instance["jobs"][job][operation])
        assert eligible[machine] == duration
        by_job[job].append((operation, start, start + duration))
        by_machine[machine].append((start, start + duration))
    for items in by_job.values():
        items.sort()
        assert all(left[2] <= right[1] for left, right in zip(items, items[1:]))
    for items in by_machine.values():
        items.sort()
        assert all(left[1] <= right[0] for left, right in zip(items, items[1:]))
    assert max(start + duration for *_, start, duration in operations) == case.solution["objective"]


def _validate_cluster_editing(case: SmokeCase) -> None:
    edges = {frozenset(edge) for edge in case.instance["edges"]}
    for edit in case.solution["edits"]:
        edge = frozenset(edit)
        edges.symmetric_difference_update({edge})
    n = case.instance["vertex_count"]
    for left in range(n):
        for middle in range(n):
            for right in range(n):
                if len({left, middle, right}) == 3 and frozenset((left, middle)) in edges and frozenset((middle, right)) in edges:
                    assert frozenset((left, right)) in edges
    assert len(case.solution["edits"]) == case.solution["objective"]


def _validate_dfvs(case: SmokeCase) -> None:
    removed = case.solution["removed"]
    _assert_indices(removed, case.instance["vertex_count"])
    remaining = set(range(case.instance["vertex_count"])) - set(removed)
    assert _is_acyclic(remaining, case.instance["arcs"])
    assert len(removed) == case.solution["objective"]


def _validate_twinwidth(case: SmokeCase) -> None:
    assert case.instance["edges"] == []
    live = set(range(case.instance["vertex_count"]))
    for keep, remove in case.solution["contractions"]:
        assert keep in live and remove in live and keep != remove
        live.remove(remove)
    assert len(live) == 1
    assert case.solution["objective"] == 0


def _validate_dominating_set(case: SmokeCase) -> None:
    selected = case.solution["selected"]
    _assert_indices(selected, case.instance["vertex_count"])
    dominated = set(selected)
    for left, right in case.instance["edges"]:
        if left in selected:
            dominated.add(right)
        if right in selected:
            dominated.add(left)
    assert dominated == set(range(case.instance["vertex_count"]))
    assert len(selected) == case.solution["objective"]


def _validate_maf(case: SmokeCase) -> None:
    trees = [tree.replace(" ", "") for tree in case.instance["trees"]]
    forest = [tree.replace(" ", "") for tree in case.solution["forest"]]
    assert len(set(trees)) == 1
    assert forest == [trees[0]]
    assert len(forest) == case.solution["objective"]


_VALIDATORS = {
    "assignment": _validate_assignment,
    "knapsack": _validate_knapsack,
    "set_cover": _validate_set_cover,
    "graph_coloring": _validate_graph_coloring,
    "steiner_tree": _validate_steiner_tree,
    "cvrp": _validate_cvrp,
    "flexible_job_shop": _validate_flexible_job_shop,
    "cluster_editing": _validate_cluster_editing,
    "directed_feedback_vertex_set": _validate_dfvs,
    "twinwidth": _validate_twinwidth,
    "dominating_set": _validate_dominating_set,
    "maximum_agreement_forest": _validate_maf,
}


def validate_smoke_inventory(inventory: Core12SmokeInventory, expected_task_ids: tuple[str, ...]) -> dict[str, str]:
    cases = {case.task_id: case for case in inventory.cases}
    assert tuple(cases) == expected_task_ids
    results: dict[str, str] = {}
    for task_id, case in cases.items():
        _VALIDATORS[task_id](case)
        results[task_id] = "PASS"
    return results
