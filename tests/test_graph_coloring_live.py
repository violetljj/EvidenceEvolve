from __future__ import annotations

import json
from pathlib import Path

from evidence_evolve.benchmarks.protocol import load_benchmark_protocol
from evidence_evolve.benchmarks.runner import ThreeArmBenchmarkRunner
from evidence_evolve.governance.protocol_lock import load_contract
from tasks.graph_coloring import real_arms


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_PROTOCOL = (
    REPO_ROOT
    / "benchmarks"
    / "graph_coloring"
    / "three_arm_live_pilot_v0.locked.yaml"
)


class FakeLiveCodexBackend:
    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        *,
        role,
        workdir: Path,
        output_schema: Path,
        output_path: Path,
        events_path: Path,
        stderr_path: Path,
        **_kwargs,
    ) -> dict[str, object]:
        self.calls += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        if role.name == "hypothesis_explorer":
            schema = json.loads(output_schema.read_text(encoding="utf-8"))
            genome = schema["$defs"]["CandidateGenome"]["properties"]
            candidate_id = genome["candidate_id"]["const"]
            island = genome["island"]["const"]
            parents = genome["parent_ids"]["items"]["enum"]
            mutation = genome["mutation_type"]["const"]
            controls = genome["required_controls"]["items"]["enum"]
            payload = {
                "acquisition": {
                    "candidate": {
                        "schema_version": "1.0",
                        "candidate_id": candidate_id,
                        "parent_ids": [parents[0]],
                        "genetic_parent_id": parents[0],
                        "island": island,
                        "family": "degree_ordering",
                        "mutation_type": mutation,
                        "search_abstraction": "direct_solution",
                        "hypothesis": "A deterministic ordering change can reduce greedy colors.",
                        "intervention": "Change only the candidate vertex ordering rule.",
                        "mechanism_claims": ["ordering changes neighborhood saturation"],
                        "assumptions": [],
                        "expected_signature": {
                            "improve": ["mean_color_count"],
                            "unchanged": ["relative_improvement"],
                        },
                        "falsifier": "The frozen development evaluator reports no reduction.",
                        "required_controls": controls,
                        "behavior_descriptor": {},
                        "ablation_plan": [],
                        "transfer_motifs": [],
                        "failure_risks": [],
                        "editable_files": [
                            "tasks/graph_coloring/candidates/baseline.py"
                        ],
                        "estimated_cost_tier": 1,
                        "estimated_information_value": 0.5,
                        "reopen_condition_claims": [],
                    },
                    "signals": {
                        "admit_probability": 1.0,
                        "expected_improvement": 0.1,
                        "information_gain": 0.5,
                        "novelty": 0.5,
                        "transfer_value": 0.0,
                        "estimated_cost": 1.0,
                        "redundancy": 0.0,
                    },
                    "verified_reopen_conditions": [],
                },
                "stage": "M0_MECHANICS",
                "reference_metrics": {},
            }
        else:
            candidate = workdir / "tasks" / "graph_coloring" / "candidates" / "baseline.py"
            candidate.write_text(
                candidate.read_text(encoding="utf-8")
                + f"\n# fake-live-call-{self.calls}\n",
                encoding="utf-8",
            )
            payload = {
                "status": "IMPLEMENTED",
                "summary": "Applied one candidate-local deterministic edit.",
                "tests": [],
            }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        events_path.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")
        return {"status": "PASS"}


def test_live_three_arm_adapter_executes_real_paths_with_fake_codex(
    tmp_path: Path, monkeypatch
) -> None:
    protocol = load_benchmark_protocol(LIVE_PROTOCOL)
    backend = FakeLiveCodexBackend()
    monkeypatch.setattr(real_arms, "_codex_backend", lambda: backend)

    result = ThreeArmBenchmarkRunner(
        protocol=protocol,
        repo_root=REPO_ROOT,
        run_dir=tmp_path / "run",
        adapter=real_arms.run_three_arm_trial,
    ).run()

    assert backend.calls == 12
    assert result.superiority_claim_permitted is False
    assert all(summary.candidate_evaluations_used == 2 for summary in result.arms)
    assert all(summary.public_fresh_valid_rate == 1.0 for summary in result.arms)
    assert all(summary.token_count_used == 120 for summary in result.arms)
    manifests = list((tmp_path / "run" / "trials").rglob("run_manifest.json"))
    memory_profiles = {
        json.loads(path.read_text(encoding="utf-8"))["scientific_memory_enabled"]
        for path in manifests
        if "campaign" in path.parts or "vanilla" in path.parts
    }
    assert memory_profiles == {False, True}
    runtime_contracts = list(
        (tmp_path / "run" / "trials").rglob("campaign_contract.locked.yaml")
    )
    assert len(runtime_contracts) == 3
    for path in runtime_contracts:
        base_commit = load_contract(path).campaign.base_commit
        assert len(base_commit) == 40
        assert all(character in "0123456789abcdef" for character in base_commit)


def test_protocol_instances_match_frozen_instance_manifest() -> None:
    protocol = load_benchmark_protocol(LIVE_PROTOCOL)
    payload = json.loads(
        (REPO_ROOT / "benchmarks" / "graph_coloring" / "instances_v0.json").read_text(
            encoding="utf-8"
        )
    )

    assert [item.model_dump(mode="json") for item in protocol.development.instances] == payload[
        "development"
    ]
    assert [item.model_dump(mode="json") for item in protocol.public_fresh.instances] == payload[
        "public_fresh"
    ]

    evidence_manifest = json.loads(
        (
            REPO_ROOT
            / "research"
            / "evidence_policies"
            / "graph_coloring_dev_manifest.json"
        ).read_text(encoding="utf-8")
    )
    contract = load_contract(
        REPO_ROOT / "research" / "contracts" / "graph_coloring_live_v0.template.yaml"
    )
    assert evidence_manifest["source_id"] == contract.evidence_sources[0].source_id
