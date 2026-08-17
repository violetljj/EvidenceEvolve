from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _usage_from_events(stdout: str) -> dict[str, int]:
    last: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict):
            last = usage
    return {
        "inputTokens": int(last.get("input_tokens", 0) or 0),
        "cacheReadTokens": int(last.get("cached_input_tokens", 0) or 0),
        "outputTokens": int(last.get("output_tokens", 0) or 0),
        "reasoningOutputTokens": int(last.get("reasoning_output_tokens", 0) or 0),
    }


def _append_usage(usage: dict[str, int]) -> None:
    configured = os.environ.get("EE_HEADLESS_USAGE_LOG")
    if not configured:
        return
    path = Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"usage": usage}, sort_keys=True) + "\n")


def _check(executable: str) -> int:
    completed = subprocess.run(
        [executable, "--version"], check=False, capture_output=True, text=True
    )
    if completed.returncode == 0:
        sys.stdout.write(completed.stdout)
    else:
        sys.stderr.write(completed.stderr or completed.stdout)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("agent", nargs="?")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--allow")
    parser.add_argument("--usage", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    args, _unknown = parser.parse_known_args()
    executable = os.environ.get("EVIDENCE_EVOLVE_CODEX_EXECUTABLE", "codex")
    if args.check:
        return _check(executable)
    if args.agent != "codex" or args.prompt_file is None or args.work_dir is None:
        parser.error("codex, --prompt-file, and --work-dir are required")

    prompt = args.prompt_file.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="ee-headless-") as temporary:
        last_message = Path(temporary) / "last-message.txt"
        command = [
            executable,
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--cd",
            str(args.work_dir),
            "--output-last-message",
            str(last_message),
        ]
        if args.model:
            command.extend(["--model", args.model])
        if args.reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{args.reasoning_effort}"'])
        command.append("-")
        completed = subprocess.run(
            command,
            cwd=args.work_dir,
            input=prompt,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            sys.stderr.write(completed.stderr or completed.stdout)
            return completed.returncode
        content = last_message.read_text(encoding="utf-8").strip()
        if not content:
            sys.stderr.write("Codex produced no final message\n")
            return 1
        usage = _usage_from_events(completed.stdout)
        _append_usage(usage)
        sys.stdout.write(content + "\n")
        sys.stdout.write(json.dumps({"usage": usage}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
