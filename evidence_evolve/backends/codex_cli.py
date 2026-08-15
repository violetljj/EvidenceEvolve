from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexRole:
    name: str
    writable: bool = False


READ_ONLY_ROLES = {
    "research_director",
    "hypothesis_explorer",
    "protocol_auditor",
    "novelty_judge",
    "result_analyst",
    "adversarial_reviewer",
    "gatekeeper",
    "meta_researcher",
}


class CodexCliBackend:
    def __init__(self, executable: str = "codex"):
        self.executable = executable

    def status(self) -> dict[str, object]:
        discovered_path = shutil.which(self.executable)
        if discovered_path is None:
            return {
                "discovered": False,
                "usable": False,
                "path": None,
                "version": None,
                "error": "EXECUTABLE_NOT_FOUND",
            }
        try:
            completed = subprocess.run(
                [self.executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "discovered": True,
                "usable": False,
                "path": discovered_path,
                "version": None,
                "error": f"{type(exc).__name__}:{exc}",
            }
        version = (completed.stdout or completed.stderr).strip() or None
        return {
            "discovered": True,
            "usable": completed.returncode == 0,
            "path": discovered_path,
            "version": version,
            "error": None if completed.returncode == 0 else f"EXIT_{completed.returncode}",
        }

    def available(self) -> bool:
        return bool(self.status()["usable"])

    def build_command(
        self,
        *,
        role: CodexRole,
        prompt: str,
        workdir: Path,
        output_schema: Path,
        output_path: Path,
    ) -> list[str]:
        if role.writable and role.name != "implementer":
            raise ValueError("only the implementer role may request workspace-write")
        if not role.writable and role.name not in READ_ONLY_ROLES:
            raise ValueError(f"unknown read-only role: {role.name}")
        sandbox = "workspace-write" if role.writable else "read-only"
        return [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            sandbox,
            "--cd",
            str(workdir),
            "--output-schema",
            str(output_schema),
            "--output-last-message",
            str(output_path),
            prompt,
        ]

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
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            events_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "", encoding="utf-8")
            return {"status": "TIMEOUT", "command": command}
        except OSError as exc:
            events_path.write_text("", encoding="utf-8")
            stderr_path.write_text(
                f"{type(exc).__name__}: {exc}", encoding="utf-8"
            )
            return {
                "status": "UNAVAILABLE",
                "command": command,
                "error": f"{type(exc).__name__}:{exc}",
            }
        events_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        event_types: list[str] = []
        for line in completed.stdout.splitlines():
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
        }
