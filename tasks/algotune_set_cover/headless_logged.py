from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    completed = subprocess.run(
        ["headless", *sys.argv[1:]],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    usage_log = os.environ.get("EE_HEADLESS_USAGE_LOG")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if usage_log and completed.returncode == 0 and lines:
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            path = Path(usage_log)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
