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

