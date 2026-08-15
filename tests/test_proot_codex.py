from pathlib import Path

import pytest

from evidence_evolve.backends.proot_codex import ProotCodexCliBackend


def test_bridge_sync_copies_only_regular_workspace_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "kept.py").write_text("before\n", encoding="utf-8")
    (destination / "kept.py").write_text("before\n", encoding="utf-8")
    (source / "deleted.py").write_text("delete\n", encoding="utf-8")
    (destination / "deleted.py").write_text("delete\n", encoding="utf-8")
    before = ProotCodexCliBackend._workspace_snapshot(source)

    (source / "kept.py").write_text("after\n", encoding="utf-8")
    (source / "deleted.py").unlink()
    (source / "nested").mkdir()
    (source / "nested" / "new.py").write_text("new\n", encoding="utf-8")
    ProotCodexCliBackend._sync_workspace_changes(
        source=source,
        destination=destination,
        before=before,
    )

    assert (destination / "kept.py").read_text(encoding="utf-8") == "after\n"
    assert not (destination / "deleted.py").exists()
    assert (destination / "nested" / "new.py").read_text(encoding="utf-8") == "new\n"


def test_bridge_sync_rejects_changed_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    before = ProotCodexCliBackend._workspace_snapshot(source)
    (source / "escape").symlink_to("/etc/passwd")

    with pytest.raises(RuntimeError, match="non-regular"):
        ProotCodexCliBackend._sync_workspace_changes(
            source=source,
            destination=destination,
            before=before,
        )
