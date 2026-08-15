#!/usr/bin/env python3
"""Experiment-only Codex shim for hosts where nested bwrap is unavailable."""

from __future__ import annotations

import os
import shutil
import sys


def main() -> None:
    executable = os.environ.get("CODEX_REAL_EXECUTABLE") or shutil.which("codex")
    if executable is None:
        raise SystemExit("codex executable not found")
    args = sys.argv[1:]
    if args and args[0] == "exec":
        rewritten = ["exec"]
        index = 1
        while index < len(args):
            if args[index] == "--sandbox" and index + 1 < len(args):
                index += 2
                continue
            rewritten.append(args[index])
            index += 1
        rewritten.insert(1, "--dangerously-bypass-approvals-and-sandbox")
        args = rewritten
    os.execv(executable, [executable, *args])


if __name__ == "__main__":
    main()
