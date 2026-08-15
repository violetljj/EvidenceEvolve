from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from evidence_evolve.backends.codex_cli import (
    CodexCliBackend,
    CodexRole,
    _utf8_text,
)


class ProotCodexCliBackend(CodexCliBackend):
    """Writable Codex bridge for hosts that forbid user namespaces.

    Read-only roles retain the shared frozen backend behavior. Implementers run
    as ``nobody`` in a minimal PRoot filesystem containing a disposable copy of
    their candidate worktree. Only regular-file changes are synchronized back.
    """

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _workspace_snapshot(cls, root: Path) -> dict[str, tuple[str, str, int]]:
        snapshot: dict[str, tuple[str, str, int]] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] == ".git":
                continue
            mode = path.lstat().st_mode
            permissions = stat.S_IMODE(mode)
            if stat.S_ISREG(mode):
                snapshot[str(relative)] = (
                    "file",
                    cls._file_digest(path),
                    permissions,
                )
            elif stat.S_ISDIR(mode):
                snapshot[str(relative)] = ("dir", "", permissions)
            elif stat.S_ISLNK(mode):
                snapshot[str(relative)] = (
                    "symlink",
                    os.readlink(path),
                    permissions,
                )
            else:
                snapshot[str(relative)] = ("special", "", permissions)
        return snapshot

    @classmethod
    def _sync_workspace_changes(
        cls,
        *,
        source: Path,
        destination: Path,
        before: dict[str, tuple[str, str, int]],
    ) -> None:
        after = cls._workspace_snapshot(source)
        for relative_text in sorted(set(before) | set(after)):
            if before.get(relative_text) == after.get(relative_text):
                continue
            relative = Path(relative_text)
            source_path = source / relative
            destination_path = destination / relative
            current = after.get(relative_text)
            if current is None:
                if destination_path.is_symlink() or destination_path.is_file():
                    destination_path.unlink()
                elif destination_path.is_dir():
                    shutil.rmtree(destination_path)
                continue
            kind = current[0]
            if kind in {"symlink", "special"}:
                raise RuntimeError(
                    "sandbox bridge refuses changed non-regular path: "
                    f"{relative_text}"
                )
            if kind == "dir":
                destination_path.mkdir(parents=True, exist_ok=True)
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if destination_path.is_symlink() or destination_path.is_dir():
                if destination_path.is_dir() and not destination_path.is_symlink():
                    shutil.rmtree(destination_path)
                else:
                    destination_path.unlink()
            shutil.copy2(source_path, destination_path)

    @staticmethod
    def _chown_tree(root: Path, uid: int, gid: int) -> None:
        os.chown(root, uid, gid, follow_symlinks=False)
        for path in root.rglob("*"):
            os.chown(path, uid, gid, follow_symlinks=False)

    @staticmethod
    def _copy_runtime_binary(source: Path, destination: Path, uid: int, gid: int) -> None:
        subprocess.run(
            [
                "cp",
                "--reflink=auto",
                "--preserve=mode,timestamps",
                str(source),
                str(destination),
            ],
            check=True,
            capture_output=True,
        )
        os.chown(destination, uid, gid, follow_symlinks=False)

    def run(
        self,
        *,
        role: CodexRole,
        prompt: str,
        workdir: Path,
        output_schema: Path,
        output_path: Path,
        events_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> dict[str, object]:
        if not role.writable:
            return super().run(
                role=role,
                prompt=prompt,
                workdir=workdir,
                output_schema=output_schema,
                output_path=output_path,
                events_path=events_path,
                stderr_path=stderr_path,
                timeout_seconds=timeout_seconds,
            )
        command = self.build_command(
            role=role,
            prompt=prompt,
            workdir=workdir,
            output_schema=output_schema,
            output_path=output_path,
        )
        events_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return self._run_writable(
            command=command,
            prompt=prompt,
            workdir=workdir,
            output_schema=output_schema,
            output_path=output_path,
            events_path=events_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
        )

    def _run_writable(
        self,
        *,
        command: list[str],
        prompt: str,
        workdir: Path,
        output_schema: Path,
        output_path: Path,
        events_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> dict[str, object]:
        proot = shutil.which("proot")
        discovered = shutil.which(self.executable)
        executable = Path(discovered).resolve() if discovered else None
        auth_path = Path.home() / ".codex" / "auth.json"
        code_mode_host = (
            executable.with_name("codex-code-mode-host") if executable else None
        )
        if proot is None:
            stderr_path.write_text("proot executable is required", encoding="utf-8")
            return {"status": "UNAVAILABLE", "command": command, "error": "PROOT_NOT_FOUND"}
        if (
            executable is None
            or code_mode_host is None
            or not code_mode_host.is_file()
            or not auth_path.is_file()
        ):
            stderr_path.write_text(
                "Codex executable, code-mode host, or auth unavailable",
                encoding="utf-8",
            )
            return {
                "status": "UNAVAILABLE",
                "command": command,
                "error": "CODEX_BRIDGE_INPUT_MISSING",
            }

        nobody = pwd.getpwnam("nobody")
        before = self._workspace_snapshot(workdir)
        with tempfile.TemporaryDirectory(prefix="ee-codex-proot-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            shutil.copytree(workdir, workspace, symlinks=True)
            git_marker = workspace / ".git"
            if git_marker.is_file() or git_marker.is_symlink():
                git_marker.unlink()
            elif git_marker.is_dir():
                shutil.rmtree(git_marker)
            common_git = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=workdir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            shutil.copytree(common_git, git_marker, symlinks=True)
            candidate_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workdir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (git_marker / "HEAD").write_text(candidate_head + "\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    f"--git-dir={git_marker}",
                    f"--work-tree={workspace}",
                    "reset",
                    "--mixed",
                    candidate_head,
                ],
                check=True,
                capture_output=True,
            )

            codex_home = root / ".codex"
            codex_home.mkdir()
            shutil.copy2(auth_path, codex_home / "auth.json")
            output_dir = root / "output"
            output_dir.mkdir()
            shutil.copy2(output_schema, output_dir / "schema.json")
            for path in (
                root / "home" / "implementer",
                root / "dev",
                root / "opt",
                root / "proc" / "self",
                root / "tmp",
                root / "etc",
            ):
                path.mkdir(parents=True, exist_ok=True)
            (root / "proc" / "self" / "exe").symlink_to("/opt/codex")
            for name in ("hosts", "nsswitch.conf", "resolv.conf"):
                source = Path("/etc") / name
                if source.is_file():
                    shutil.copy2(source, root / "etc" / name)
            if Path("/etc/ssl/certs").is_dir():
                (root / "etc" / "ssl").mkdir()
                shutil.copytree(
                    "/etc/ssl/certs",
                    root / "etc" / "ssl" / "certs",
                    symlinks=True,
                )
            for link, target in {
                "bin": "usr/bin",
                "lib": "usr/lib",
                "lib64": "usr/lib64",
                "sbin": "usr/sbin",
            }.items():
                (root / link).symlink_to(target)
            self._chown_tree(root, nobody.pw_uid, nobody.pw_gid)
            self._copy_runtime_binary(
                executable, root / "opt" / "codex", nobody.pw_uid, nobody.pw_gid
            )
            self._copy_runtime_binary(
                code_mode_host,
                root / "opt" / "codex-code-mode-host",
                nobody.pw_uid,
                nobody.pw_gid,
            )

            bridged = list(command)
            bridged[0] = "/opt/codex"
            sandbox_index = bridged.index("--sandbox")
            del bridged[sandbox_index : sandbox_index + 2]
            bridged.insert(sandbox_index, "--dangerously-bypass-approvals-and-sandbox")
            bridged[bridged.index("--cd") + 1] = "/workspace"
            bridged[bridged.index("--output-schema") + 1] = "/output/schema.json"
            bridged[bridged.index("--output-last-message") + 1] = "/output/result.json"
            bridged[-1:-1] = [
                "--ignore-user-config",
                "--disable",
                "apps",
                "--disable",
                "plugins",
            ]
            proxy_environment = [
                f"{name}={os.environ[name]}"
                for name in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                )
                if os.environ.get(name)
            ]
            bridge_command = [
                "/usr/bin/setpriv",
                f"--reuid={nobody.pw_uid}",
                f"--regid={nobody.pw_gid}",
                "--clear-groups",
                "--no-new-privs",
                proot,
                "-r",
                str(root),
                "-b",
                "/usr:/usr",
                "-b",
                "/dev/null:/dev/null",
                "-b",
                "/dev/urandom:/dev/urandom",
                "-w",
                "/workspace",
                "/usr/bin/env",
                "-i",
                "HOME=/home/implementer",
                "CODEX_HOME=/.codex",
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG=C.UTF-8",
                *proxy_environment,
                *bridged,
            ]
            try:
                completed = subprocess.run(
                    bridge_command,
                    cwd="/tmp",
                    check=False,
                    capture_output=True,
                    input=prompt,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                events_path.write_text(_utf8_text(exc.stdout), encoding="utf-8")
                stderr_path.write_text(_utf8_text(exc.stderr), encoding="utf-8")
                return {
                    "status": "TIMEOUT",
                    "command": command,
                    "sandbox_bridge": "proot-nobody",
                }
            stdout = _utf8_text(completed.stdout)
            stderr = _utf8_text(completed.stderr)
            events_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            bridge_output = output_dir / "result.json"
            if completed.returncode == 0 and bridge_output.is_file():
                self._sync_workspace_changes(
                    source=workspace,
                    destination=workdir,
                    before=before,
                )
                shutil.copy2(bridge_output, output_path)
            event_types: list[str] = []
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("type"), str):
                    event_types.append(event["type"])
            return {
                "status": "PASS" if completed.returncode == 0 else "FAIL",
                "returncode": completed.returncode,
                "command": command,
                "event_types": event_types,
                "sandbox_bridge": "proot-nobody",
            }
