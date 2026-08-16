from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter


PALETTE = {
    "shinka": "#355C7D",
    "ada": "#C58B16",
    "evox": "#D5663F",
    "evidence_evolve": "#6B7D3A",
}
LABELS = {
    "shinka": "Shinka",
    "ada": "AdaEvolve",
    "evox": "EvoX",
    "evidence_evolve": "EvidenceEvolve",
}
MARKERS = {
    "shinka": "o",
    "ada": "s",
    "evox": "D",
    "evidence_evolve": "^",
}


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in result["results"]:
        valid = item["scientific_outcome"] not in {
            "NOT_EVALUABLE_DATA",
            "INVALID_MECHANICS_OR_ADAPTER",
        }
        rows.append(
            {
                "arm": item["arm"],
                "horizon": int(item["horizon"]),
                "tokens": int(item["cumulative_tokens"]),
                "search_wall_seconds": float(item["cumulative_wall_seconds"]),
                "development_speedup": float(item["development_raw_speedup"]),
                "heldout_speedup": (
                    float(item["heldout"]["raw_speedup"]) if valid else None
                ),
                "heldout_valid_rate": float(item["heldout"]["valid_rate"]),
                "scientific_outcome": item["scientific_outcome"],
                "candidate_sha256": item["candidate_sha256"],
            }
        )
    return sorted(rows, key=lambda row: (row["arm"], row["horizon"]))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _axis_value(row: dict[str, Any], field: str) -> float:
    value = float(row[field])
    return value / 3600.0 if field == "search_wall_seconds" else value


def plot(result_path: Path, output_dir: Path) -> dict[str, Any]:
    result_path = result_path.resolve()
    output_dir = output_dir.resolve()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    rows = _rows(result)
    _write_csv(output_dir / "curve.csv", rows)

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.edgecolor": "#3E454D",
            "axes.labelcolor": "#252A31",
            "text.color": "#252A31",
            "xtick.color": "#4F5862",
            "ytick.color": "#4F5862",
        }
    )
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 5.6), sharey=True)
    panels = (
        ("horizon", "Search horizon", [3, 6, 12, 24, 50]),
        ("tokens", "Cumulative tokens", [50_000, 100_000, 300_000, 1_000_000, 3_000_000, 10_000_000]),
        ("search_wall_seconds", "Cumulative search wall time (hours)", [0.1, 0.25, 0.5, 1, 2, 3]),
    )
    for axis, (field, title, ticks) in zip(axes, panels, strict=True):
        for arm in ("shinka", "ada", "evox", "evidence_evolve"):
            arm_rows = [row for row in rows if row["arm"] == arm]
            x = [_axis_value(row, field) for row in arm_rows]
            development = [row["development_speedup"] for row in arm_rows]
            heldout = [row["heldout_speedup"] for row in arm_rows]
            axis.plot(
                x,
                development,
                color=PALETTE[arm],
                linestyle=(0, (2, 2)),
                linewidth=1.4,
                alpha=0.48,
                marker=MARKERS[arm],
                markersize=5,
                markerfacecolor="white",
                markeredgewidth=1.0,
                zorder=2,
            )
            axis.plot(
                x,
                heldout,
                color=PALETTE[arm],
                linestyle="-",
                linewidth=2.2,
                marker=MARKERS[arm],
                markersize=6,
                markerfacecolor=PALETTE[arm],
                markeredgecolor="white",
                markeredgewidth=0.7,
                zorder=3,
            )
            for x_value, row in zip(x, arm_rows, strict=True):
                if row["heldout_speedup"] is None:
                    axis.scatter(
                        [x_value],
                        [0.84],
                        color="#20252B",
                        marker="X",
                        s=72,
                        linewidths=0.8,
                        zorder=5,
                    )
                    axis.annotate(
                        "invalid\n(no score)",
                        (x_value, 0.84),
                        xytext=(0, 11),
                        textcoords="offset points",
                        ha="center",
                        va="bottom",
                        fontsize=8,
                        color="#20252B",
                    )

        axis.set_title(title, loc="left", fontsize=11, fontweight="bold")
        axis.set_xscale("log", base=2 if field != "tokens" else 10)
        axis.set_xticks(ticks)
        if field == "tokens":
            axis.xaxis.set_major_formatter(
                FuncFormatter(lambda value, _position: f"{value / 1_000_000:g}M" if value >= 1_000_000 else f"{value / 1_000:g}k")
            )
        else:
            axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{value:g}"))
        axis.set_yscale("log", base=2)
        axis.set_ylim(0.8, 16)
        axis.set_yticks([1, 2, 4, 8, 16])
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{value:g}×"))
        axis.axhline(1.0, color="#6F7780", linewidth=1.0, linestyle=(0, (4, 3)), zorder=1)
        axis.grid(axis="y", color="#DDE1E5", linewidth=0.8, alpha=0.9)
        axis.grid(axis="x", visible=False)
        axis.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Solver speedup vs frozen reference")

    arm_handles = [
        Line2D(
            [0],
            [0],
            color=PALETTE[arm],
            marker=MARKERS[arm],
            linewidth=2.2,
            label=LABELS[arm],
        )
        for arm in ("shinka", "ada", "evox", "evidence_evolve")
    ]
    state_handles = [
        Line2D([0], [0], color="#252A31", linewidth=2.2, label="Held-out"),
        Line2D(
            [0],
            [0],
            color="#6F7780",
            linewidth=1.4,
            linestyle=(0, (2, 2)),
            marker="o",
            markerfacecolor="white",
            label="Development",
        ),
        Line2D([0], [0], color="#20252B", marker="X", linewidth=0, label="Invalid held-out"),
    ]
    fig.legend(
        handles=arm_handles + state_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.895),
        ncol=7,
        frameon=False,
        fontsize=9,
    )
    fig.suptitle(
        "Set Cover speedup across frozen search-budget checkpoints",
        x=0.06,
        y=0.975,
        ha="left",
        fontsize=15,
        fontweight="bold",
    )
    fig.text(
        0.06,
        0.925,
        "Held-out: 100 OS-CSPRNG seeds × 10 repeats. Log axes preserve multiplicative scaling; 1× is the reference solver.",
        ha="left",
        fontsize=9.5,
        color="#4F5862",
    )
    fig.text(
        0.06,
        0.025,
        "EvoX h24 is excluded from speedup comparisons: 990/1000 PASS and 10 INVALID_SOLUTION. Development lines are context, not held-out evidence.",
        ha="left",
        fontsize=9,
        color="#4F5862",
    )
    fig.subplots_adjust(left=0.065, right=0.985, bottom=0.16, top=0.79, wspace=0.18)
    output_dir.mkdir(parents=True, exist_ok=True)
    png = output_dir / "scaling_curves.png"
    svg = output_dir / "scaling_curves.svg"
    fig.savefig(png, dpi=180, facecolor="white")
    fig.savefig(svg, facecolor="white")
    plt.close(fig)
    return {
        "source_result": str(result_path),
        "row_count": len(rows),
        "png": str(png),
        "svg": str(svg),
        "csv": str(output_dir / "curve.csv"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot Set Cover blind horizon scaling")
    parser.add_argument("result", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(plot(args.result, args.output_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
