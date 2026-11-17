"""Render README charts from a completed benchmark's samples.csv and report.json."""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .dataset import load_dataset

ROOT = Path(__file__).resolve().parent
SERVICES = {"in_memory": "InMemory", "null": "Null"}
METRICS = {
    "delta_traced_current_bytes": "Python allocations · tracing on",
    "delta_rss_bytes": "RSS · tracing off",
}
THEMES = {
    "light": ("#ffffff", "#202938", "#dce2eb", "#8294ad", "#7040ce"),
    "dark": ("#0d1117", "#e6edf3", "#30363d", "#93a6c0", "#b596ff"),
}


def load_data(directory):
    report, rows = load_dataset(directory)
    if set(report["config"]["measurement_modes"]) != {"rss", "tracemalloc"}:
        raise ValueError(
            "Both independent measurement modes are required for these charts"
        )
    series = {}
    for service in SERVICES:
        series[service] = {}
        for mode, metric in (
            ("tracemalloc", "delta_traced_current_bytes"),
            ("rss", "delta_rss_bytes"),
        ):
            groups = {}
            for row in rows:
                if row["service"] == service and row["measurement_mode"] == mode:
                    groups.setdefault(int(row["requests"]), []).append(int(row[metric]))
            for count, values in sorted(groups.items()):
                series[service].setdefault(count, {})[metric] = tuple(
                    value / 1024**2
                    for value in (statistics.median(values), min(values), max(values))
                )
    return report, series


def save(fig, output, name, theme, preview_dir):
    suffix = "-dark" if theme == "dark" else ""
    path = output / f"{name}{suffix}.svg"
    fig.savefig(path, metadata={"Date": None})
    svg = path.read_text(encoding="utf-8")
    path.write_text(
        "\n".join(line.rstrip() for line in svg.splitlines()) + "\n", encoding="utf-8"
    )
    if preview_dir:
        preview_dir.mkdir(parents=True, exist_ok=True)
        fig.savefig(preview_dir / f"{name}{suffix}.png", dpi=140)
    plt.close(fig)


def render(report, series, output, theme, preview_dir):
    background, text, grid, memory_color, null_color = THEMES[theme]
    colors = {"in_memory": memory_color, "null": null_color}
    config = report["config"]
    count = config["requests"]
    subtitle = (
        f"{count:,} requests · {config['concurrency']} concurrent workers · "
        f"median of {config['repeats']} runs · lower is better"
    )
    with plt.rc_context(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "figure.facecolor": background,
            "axes.facecolor": background,
            "text.color": text,
            "axes.labelcolor": text,
            "xtick.color": text,
            "ytick.color": text,
            "axes.edgecolor": grid,
            "svg.fonttype": "none",
            "svg.hashsalt": "adk-null-session-benchmark",
        }
    ):
        fig, axes = plt.subplots(1, 2, figsize=(12, 3.7))
        fig.subplots_adjust(left=0.10, right=0.97, top=0.70, bottom=0.26, wspace=0.36)
        fig.text(
            0.05,
            0.91,
            f"Memory growth after {count:,} A2A requests",
            fontsize=20,
            weight="bold",
        )
        fig.text(0.05, 0.82, subtitle, fontsize=11)
        for ax, (metric, title) in zip(axes, METRICS.items(), strict=True):
            maximum = max(series[s][count][metric][2] for s in SERVICES)
            for index, service in enumerate(SERVICES):
                median, low, high = series[service][count][metric]
                ax.barh(index, median, height=0.50, color=colors[service], zorder=3)
                ax.errorbar(
                    median,
                    index,
                    xerr=[[median - low], [high - median]],
                    fmt="none",
                    ecolor=text,
                    capsize=4,
                    linewidth=1.3,
                    zorder=4,
                )
                ax.text(
                    high + maximum * 0.035,
                    index,
                    f"+{median:.2f} MiB",
                    va="center",
                    fontsize=12,
                    weight="bold",
                    color=colors[service],
                )
            ax.set_yticks(range(len(SERVICES)), SERVICES.values())
            ax.set_ylim(1.65, -0.65)
            ax.set_xlim(0, maximum * 1.43)
            ax.set_title(title, loc="left", pad=12, fontsize=13, weight="bold")
            ax.set_xlabel("Increase from warmup baseline (MiB)", fontsize=10)
            ax.set_axisbelow(True)
            ax.grid(axis="x", color=grid, alpha=0.7)
            ax.tick_params(axis="both", length=0, pad=8)
            for spine in ax.spines.values():
                spine.set_visible(False)
        fig.text(
            0.05,
            0.07,
            "Bars: median · whiskers: min–max · separate processes per mode · scripted LLM · A2A tasks retained",
            fontsize=9,
            color=text,
        )
        save(fig, output, "memory-comparison", theme, preview_dir)

        fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
        fig.subplots_adjust(left=0.07, right=0.98, top=0.68, bottom=0.23, wspace=0.24)
        fig.text(
            0.05,
            0.91,
            "Memory growth as requests accumulate",
            fontsize=20,
            weight="bold",
        )
        fig.text(0.05, 0.83, subtitle, fontsize=11)
        for ax, (metric, title) in zip(axes, METRICS.items(), strict=True):
            for service, label in SERVICES.items():
                counts = list(series[service])
                values = [series[service][n][metric] for n in counts]
                median, low, high = zip(*values, strict=True)
                ax.fill_between(
                    counts, low, high, color=colors[service], alpha=0.16, linewidth=0
                )
                ax.plot(
                    counts, median, color=colors[service], label=label, linewidth=2.8
                )
            ax.set_title(title, loc="left", fontsize=13, weight="bold", pad=12)
            ax.set_xlabel("Completed requests")
            ax.set_ylabel("Increase from baseline (MiB)", fontsize=10)
            ax.set_xlim(0, count)
            ax.set_ylim(bottom=0)
            ax.grid(color=grid, alpha=0.7)
            ax.set_axisbelow(True)
            ax.legend(frameon=False, labelcolor=text, loc="upper left")
            ax.spines[["top", "right"]].set_visible(False)
        fig.text(
            0.05,
            0.06,
            "Lines: median · bands: min–max across runs · GC after each batch · "
            f"{config['warmup']} warmup requests excluded",
            fontsize=9,
        )
        save(fig, output, "memory-growth", theme, preview_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, nargs="?", default=ROOT / "data/2026-11-18"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "assets")
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args()
    report, series = load_data(args.directory)
    args.output.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        render(report, series, args.output, theme, args.preview_dir)
    print(f"Charts: {args.output}")


if __name__ == "__main__":
    main()
