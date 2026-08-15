from pathlib import Path

from evidence_evolve.canary import run_canary
from evidence_evolve.governance.protocol_lock import ProtocolLock, dump_contract, load_contract
from evidence_evolve.replay import replay_evaluation, replay_verdict, receipt_paths


def test_synthetic_evaluation_replay_reexecutes_frozen_evaluator(tmp_path) -> None:
    repo = Path.cwd().resolve()
    draft = load_contract(repo / "research/contracts/synthetic_canary_r0.draft.yaml")
    locked = ProtocolLock(repo).lock(draft)
    contract_path = tmp_path / "synthetic.locked.yaml"
    dump_contract(locked, contract_path)
    run_dir = tmp_path / "run"

    summary = run_canary(contract_path, repo, run_dir)
    resumed = run_canary(contract_path, repo, run_dir)

    assert summary["passed"] is True
    assert resumed["passed"] is True
    assert resumed["archive"]["total"] == 5
    first_receipt = receipt_paths(run_dir)[0]
    first_receipt.with_suffix(".mechanism.json").write_text("{}\n", encoding="utf-8")
    assert len(receipt_paths(run_dir)) == 5
    assert replay_verdict(run_dir, repo) == {
        "mode": "VERDICT_REPLAY",
        "passed": True,
        "replayed": 5,
        "failures": [],
    }
    evaluation = replay_evaluation(run_dir, repo)
    assert evaluation["passed"] is True
    assert evaluation["replayed"] == 5
    assert evaluation["failures"] == []
