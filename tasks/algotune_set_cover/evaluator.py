from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tasks.algotune_set_cover.common import DEVELOPMENT_SEEDS, evaluate_candidate


def evaluate(program_path: str) -> dict[str, object]:
    count = int(os.environ.get("EE_ALGOTUNE_DEV_COUNT", len(DEVELOPMENT_SEEDS)))
    repeats = int(os.environ.get("EE_ALGOTUNE_DEV_REPEATS", "3"))
    result = evaluate_candidate(
        program_path,
        DEVELOPMENT_SEEDS[:count],
        repeats=repeats,
    )
    result["text_feedback"] = (
        f"correct={result['correct']} valid_rate={result['valid_rate']:.3f} "
        f"raw_speedup={result['raw_speedup']:.4f} failure={result['failure']}"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--program_path", required=True)
    parser.add_argument("--results_dir", required=True)
    args = parser.parse_args()
    result = evaluate(args.program_path)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "metrics.json").write_text(
        json.dumps(result, sort_keys=True), encoding="utf-8"
    )
    (results_dir / "correct.json").write_text(
        json.dumps(
            {"correct": bool(result["correct"]), "error": result["failure"]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
