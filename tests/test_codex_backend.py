from pathlib import Path

import pytest

from evidence_evolve.backends.codex_cli import CodexCliBackend, CodexRole


def test_read_only_role_gets_read_only_sandbox() -> None:
    command = CodexCliBackend().build_command(
        role=CodexRole("hypothesis_explorer"),
        prompt="propose",
        workdir=Path("repo"),
        output_schema=Path("candidate.schema.json"),
        output_path=Path("candidate.json"),
    )
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--json" in command
    assert "--output-schema" in command


def test_only_implementer_can_write() -> None:
    backend = CodexCliBackend()
    with pytest.raises(ValueError):
        backend.build_command(
            role=CodexRole("gatekeeper", writable=True),
            prompt="change the gate",
            workdir=Path("repo"),
            output_schema=Path("schema.json"),
            output_path=Path("output.json"),
        )
    command = backend.build_command(
        role=CodexRole("implementer", writable=True),
        prompt="implement",
        workdir=Path("repo"),
        output_schema=Path("schema.json"),
        output_path=Path("output.json"),
    )
    assert command[command.index("--sandbox") + 1] == "workspace-write"


def test_discovered_but_unstartable_codex_is_not_available(monkeypatch) -> None:
    monkeypatch.setattr(
        "evidence_evolve.backends.codex_cli.shutil.which",
        lambda _: "C:/Program Files/Codex/codex.exe",
    )

    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(
        "evidence_evolve.backends.codex_cli.subprocess.run",
        denied,
    )
    status = CodexCliBackend().status()
    assert status["discovered"] is True
    assert status["usable"] is False
    assert CodexCliBackend().available() is False


def test_run_reports_unavailable_executable(monkeypatch, tmp_path) -> None:
    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(
        "evidence_evolve.backends.codex_cli.subprocess.run",
        denied,
    )
    result = CodexCliBackend().run(
        role=CodexRole("hypothesis_explorer"),
        prompt="propose",
        workdir=tmp_path,
        output_schema=tmp_path / "schema.json",
        output_path=tmp_path / "candidate.json",
        events_path=tmp_path / "events.jsonl",
        stderr_path=tmp_path / "stderr.log",
        timeout_seconds=1,
    )
    assert result["status"] == "UNAVAILABLE"
    assert "PermissionError" in (tmp_path / "stderr.log").read_text(encoding="utf-8")
