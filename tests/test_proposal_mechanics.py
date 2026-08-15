from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evidence_evolve.hashing import sha256_file
from evidence_evolve.proposals.admission import run_mechanics_admission
from evidence_evolve.proposals.materializer import (
    ProposalMaterializationError,
    extract_search_replace_proposal,
    materialize_proposal,
)
from evidence_evolve.proposals.models import MatchMode
from evidence_evolve.proposals.models import ProposalMaterializerMode
from evidence_evolve.proposals.shinka_adapter import (
    apply_evidence_diff_patch,
    installed_shinka_materializer,
)


TARGET = '''# EVOLVE-BLOCK-START
def score():

    value = 1.0

    return value
# EVOLVE-BLOCK-END

IMMUTABLE = "keep"
'''


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _response(search: str, replace: str) -> str:
    return (
        "<NAME>test</NAME>\n<DIFF>\n<<<<<<< SEARCH\n"
        f"{search}\n=======\n{replace}\n>>>>>>> REPLACE\n</DIFF>"
    )


def _proposal(raw_response: str, target: str = TARGET):
    return extract_search_replace_proposal(
        proposal_id="test-proposal",
        source_system="test",
        raw_response=raw_response,
        target_sha256=_sha256_text(target),
    )


def test_materializer_admits_only_unique_blank_line_normalization() -> None:
    raw = _response(
        "def score():\n    value = 1.0\n    return value",
        "def score():\n    value = 2.0\n    return value",
    )
    candidate, receipt = materialize_proposal(_proposal(raw), TARGET.encode())

    assert "value = 2.0" in candidate
    assert 'IMMUTABLE = "keep"' in candidate
    assert receipt.applied_edits[0].match_mode == MatchMode.IGNORE_BLANK_LINES_UNIQUE


def test_materializer_prefers_an_exact_unique_match() -> None:
    raw = _response("    value = 1.0", "    value = 2.0")
    _, receipt = materialize_proposal(_proposal(raw), TARGET.encode())

    assert receipt.applied_edits[0].match_mode == MatchMode.EXACT_UNIQUE


@pytest.mark.parametrize(
    ("target", "search", "code"),
    [
        (
            "# EVOLVE-BLOCK-START\nx = 1\nx = 1\n# EVOLVE-BLOCK-END\n",
            "x = 1",
            "AMBIGUOUS_SEARCH",
        ),
        (TARGET, "   value = 1.0", "SEARCH_NOT_FOUND"),
        (TARGET, 'IMMUTABLE = "keep"', "IMMUTABLE_EDIT_REJECTED"),
    ],
)
def test_materializer_rejects_unsafe_matches(
    target: str, search: str, code: str
) -> None:
    proposal = _proposal(_response(search, "x = 2"), target)

    with pytest.raises(ProposalMaterializationError) as captured:
        materialize_proposal(proposal, target.encode())

    assert captured.value.code == code


def test_materializer_rejects_target_hash_drift() -> None:
    proposal = _proposal(_response("    value = 1.0", "    value = 2.0"))

    with pytest.raises(ProposalMaterializationError) as captured:
        materialize_proposal(proposal, (TARGET + "# drift\n").encode())

    assert captured.value.code == "TARGET_HASH_MISMATCH"


def test_shinka_adapter_uses_materializer_and_emits_provenance(
    tmp_path: Path,
) -> None:
    pytest.importorskip("shinka")
    patch_dir = tmp_path / "patch"
    raw = _response(
        "def score():\n    value = 1.0\n    return value",
        "def score():\n    value = 2.0\n    return value",
    )

    candidate, applied, output, error, _, patch_path = apply_evidence_diff_patch(
        patch_str=raw,
        original_str=TARGET,
        patch_dir=patch_dir,
    )

    assert error is None
    assert applied == 1
    assert "value = 2.0" in candidate
    assert output == patch_dir / "main.py"
    assert patch_path == patch_dir / "search_replace.txt"
    assert (patch_dir / "proposal_ir.json").is_file()
    assert (patch_dir / "materialization_receipt.json").is_file()


def test_shinka_materializer_context_restores_upstream_function() -> None:
    async_apply = pytest.importorskip("shinka.edit.async_apply")
    original = async_apply.apply_diff_patch

    with installed_shinka_materializer(
        ProposalMaterializerMode.EVIDENCE_EVOLVE_V1
    ):
        assert async_apply.apply_diff_patch is apply_evidence_diff_patch

    assert async_apply.apply_diff_patch is original


def test_parallel_admission_records_the_candidate_survival_funnel(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target_path = repo / "target.py"
    target_path.write_text(TARGET)
    evaluator_path = repo / "evaluator.py"
    evaluator_path.write_text(
        "import argparse, importlib.util, json\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--program_path', required=True)\n"
        "p.add_argument('--results_dir', required=True)\n"
        "a = p.parse_args()\n"
        "spec = importlib.util.spec_from_file_location('candidate', a.program_path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "out = Path(a.results_dir); out.mkdir(parents=True)\n"
        "(out / 'metrics.json').write_text(json.dumps({'combined_score': module.score()}))\n"
        "(out / 'correct.json').write_text(json.dumps({'correct': True}))\n"
    )
    response = _response(
        "def score():\n    value = 1.0\n    return value",
        "def score():\n    value = 2.0\n    return value",
    )
    cases = []
    for case_id, arm in (("official-case", "official"), ("native-case", "native")):
        cases.append(
            {
                "case_id": case_id,
                "arm": arm,
                "run_id": case_id,
                "generation": 1,
                "target_program_sha256": _sha256_text(TARGET),
                "raw_llm_response": response,
                "raw_llm_response_sha256": _sha256_text(response),
                "extracted_patch_text": response,
                "extracted_patch_sha256": _sha256_text(response),
                "failure_sha256": "0" * 64,
            }
        )
    corpus_path = repo / "cases.jsonl"
    corpus_path.write_text("".join(json.dumps(case) + "\n" for case in cases))
    protocol = {
        "protocol_id": "TEST_MECHANICS_R0",
        "source_campaign": "TEST_R0",
        "corpus_path": "cases.jsonl",
        "corpus_sha256": sha256_file(corpus_path),
        "target_program_path": "target.py",
        "target_program_sha256": sha256_file(target_path),
        "evaluator_path": "evaluator.py",
        "evaluator_sha256": sha256_file(evaluator_path),
        "baseline_score": 1.0,
        "case_ids": [case["case_id"] for case in cases],
        "thresholds": {
            "patch_apply_success_rate_min": 0.9,
            "candidate_compile_success_rate_min": 0.9,
            "evaluator_reached_rate_min": 0.8,
            "nonbaseline_candidate_score_required_per_arm": 1,
        },
        "evaluator_timeout_seconds": 30,
        "remote_model_calls_permitted": False,
    }
    protocol_path = repo / "protocol.json"
    protocol_path.write_text(json.dumps(protocol))

    receipt = run_mechanics_admission(
        protocol_path=protocol_path,
        repo=repo,
        run_dir=tmp_path / "run",
        max_workers=2,
    )

    assert receipt.mechanics_status == "PASS"
    assert receipt.remote_model_calls == 0
    assert set(receipt.implementation_hashes) == {
        "admission.py",
        "materializer.py",
        "models.py",
        "shinka_adapter.py",
    }
    assert receipt.funnel.proposals == 2
    assert receipt.funnel.patchable == 2
    assert receipt.funnel.runnable == 2
    assert receipt.funnel.evaluator_reached == 2
    assert receipt.funnel.evaluator_valid == 2
    assert receipt.funnel.nonbaseline_score == 2
    assert receipt.per_arm_funnel["official"].nonbaseline_score == 1
    assert receipt.per_arm_funnel["native"].nonbaseline_score == 1
    assert (tmp_path / "run" / "mechanics_admission_receipt.json").is_file()


def test_admission_rejects_frozen_corpus_drift(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    corpus = repo / "cases.jsonl"
    corpus.write_text("{}\n")
    target = repo / "target.py"
    target.write_text(TARGET)
    evaluator = repo / "evaluator.py"
    evaluator.write_text("pass\n")
    protocol = {
        "protocol_id": "TEST_MECHANICS_R0",
        "source_campaign": "TEST_R0",
        "corpus_path": "cases.jsonl",
        "corpus_sha256": "0" * 64,
        "target_program_path": "target.py",
        "target_program_sha256": sha256_file(target),
        "evaluator_path": "evaluator.py",
        "evaluator_sha256": sha256_file(evaluator),
        "baseline_score": 1.0,
        "case_ids": ["case"],
        "thresholds": {
            "patch_apply_success_rate_min": 0.9,
            "candidate_compile_success_rate_min": 0.9,
            "evaluator_reached_rate_min": 0.8,
            "nonbaseline_candidate_score_required_per_arm": 1,
        },
        "evaluator_timeout_seconds": 30,
        "remote_model_calls_permitted": False,
    }
    protocol_path = repo / "protocol.json"
    protocol_path.write_text(json.dumps(protocol))

    with pytest.raises(ValueError, match="artifact hash mismatch"):
        run_mechanics_admission(
            protocol_path=protocol_path,
            repo=repo,
            run_dir=tmp_path / "run",
        )
