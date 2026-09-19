#!/usr/bin/env python
"""Plot the expected human solve time of the v2 task set.

Reads the task-analysis JSON written by plot_difficulty_and_types.py (newest
file in operation/results/v2/task_analysis/ unless --results is given) and
draws solve_time.{png,pdf} under operation/results/v2/plots/solve_time/:

  Left   -- histogram of case_classification.time_range buckets, shortest to
            longest, each bar stacked by difficulty. Shows the right skew:
            most tasks sit under 4 h and a thin tail runs past 20 h.
  Right  -- every task's case_classification.time_assumption_h as a dot,
            grouped by tasks.human_difficulty_measure on a log axis, with the
            bucket median drawn as a bar and labelled. Shows that Hard is a
            wider regime rather than the next even step.

Colours and typeface come from operation/v2/style_guide.yaml through the
shared helpers in plot_difficulty_and_types.py. Run with the shared plotting
environment, ~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_solve_time.py
    python operation/v2/paper_scripts/plot_solve_time.py --results operation/results/v2/task_analysis/<stamp>.json
    python operation/v2/paper_scripts/plot_solve_time.py --plot-dir /tmp/plots
"""

import argparse
import json
import sys
from pathlib import Path
from statistics import median

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    DIFFICULTY_COLORS, INK, LABEL_FS, LEGEND_FS, MUTED, RESULTS_DIR, SURFACE,
    TICK_FS, TITLE_FS, UNKNOWN_COLOR, VALUE_FS, _RoundedHandler, squircle_bar_ends,
    style_axis,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "solve_time"
PLOT_NAME = "solve_time"

# ============================================================================
# FIGURE STYLE
# ============================================================================
FIG_SIZE = (18, 8)
WIDTH_RATIOS = [1.5, 1]  # [time-range histogram, dots by difficulty]
WSPACE = 0.3
SAVE_DPI = 200
RANGE_LABEL_ROTATION = 40  # degrees; the time_range strings are wide

DOT_SIZE = 90  # scatter marker area in points^2 (~9 pt across)
DOT_ALPHA = 0.85
JITTER = 0.16  # half-width of the horizontal jitter, in category units
MEDIAN_HALF_WIDTH = 0.3  # half-width of the median bar, in category units
MEDIAN_LINE_PT = 3
LOG_TICKS = (1, 2, 5, 10, 20, 50)  # hours shown on the log axis
JITTER_SEED = 0  # fixed so the figure is reproducible


def newest_results() -> Path:
    files = sorted(RESULTS_DIR.glob("*.json"))
    if not files:
        sys.exit(
            f"no task-analysis JSON under {RESULTS_DIR}; run "
            "plot_difficulty_and_types.py first or pass --results"
        )
    return files[-1]


def plot_time_ranges(ax, report: dict) -> None:
    ranges = list(report["solve_time"]["time_range"])
    difficulties = list(report["difficulty"])
    tasks = report["tasks"]
    x = np.arange(len(ranges))
    bottom = np.zeros(len(ranges))
    outer = [None] * len(ranges)
    handles = []
    for d in difficulties:
        counts = np.array(
            [sum(1 for t in tasks if t["time_range"] == r and t["difficulty"] == d) for r in ranges]
        )
        bars = ax.bar(
            x, counts, bottom=bottom, width=0.68, label=d,
            color=DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR),
            edgecolor=SURFACE, linewidth=1.0, zorder=2,
        )
        handles.append(bars[0])
        for i, (rect, c) in enumerate(zip(bars, counts)):
            if c > 0:
                outer[i] = rect
        bottom = bottom + counts
    for xi, total in zip(x, bottom):
        ax.text(
            xi, total + bottom.max() * 0.015, str(int(total)),
            ha="center", va="bottom", fontsize=VALUE_FS, color=INK,
        )
    style_axis(ax, horizontal=False)
    ax.set_xticks(
        x, ranges, fontsize=TICK_FS, color=INK,
        rotation=RANGE_LABEL_ROTATION, ha="right", rotation_mode="anchor",
    )
    ax.set_ylim(0, bottom.max() * 1.15)
    ax.set_ylabel("Tasks", fontsize=LABEL_FS, color=MUTED)
    ax.set_xlabel("Expected solve time", fontsize=LABEL_FS, color=MUTED, labelpad=10)
    ax.set_title("Expected solve time distribution", fontsize=TITLE_FS, color=INK, pad=14)
    ax.legend(
        handles=handles, labels=difficulties, title="Difficulty", loc="upper right",
        fontsize=LEGEND_FS, title_fontsize=LEGEND_FS, frameon=False, labelcolor=INK,
        handler_map={mpatches.Rectangle: _RoundedHandler()},
    )
    squircle_bar_ends(ax, [r for r in outer if r is not None], horizontal=False)


def plot_hours_by_difficulty(ax, report: dict) -> None:
    difficulties = list(report["difficulty"])
    tasks = report["tasks"]
    rng = np.random.default_rng(JITTER_SEED)
    for i, d in enumerate(difficulties):
        hours = np.array([t["time_h"] for t in tasks if t["difficulty"] == d])
        color = DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR)
        xs = i + rng.uniform(-JITTER, JITTER, len(hours))
        ax.scatter(
            xs, hours, s=DOT_SIZE, color=color, alpha=DOT_ALPHA,
            edgecolor=SURFACE, linewidth=1.0, zorder=3,
        )
        med = median(hours)
        ax.plot(
            [i - MEDIAN_HALF_WIDTH, i + MEDIAN_HALF_WIDTH], [med, med],
            color=INK, linewidth=MEDIAN_LINE_PT, solid_capstyle="round", zorder=4,
        )
        ax.text(
            i + MEDIAN_HALF_WIDTH + 0.05, med, f"{med:.1f} h",
            ha="left", va="center", fontsize=VALUE_FS, color=INK, zorder=4,
        )
    style_axis(ax, horizontal=False)
    ax.set_yscale("log")
    ax.set_yticks(LOG_TICKS, [str(t) for t in LOG_TICKS])
    ax.yaxis.set_minor_locator(plt.NullLocator())
    ax.set_xticks(range(len(difficulties)), difficulties, fontsize=TICK_FS, color=INK)
    ax.set_xlim(-0.6, len(difficulties) - 0.4)
    ax.set_ylabel("Hours (log scale)", fontsize=LABEL_FS, color=MUTED)
    ax.set_title("Solve time by difficulty", fontsize=TITLE_FS, color=INK, pad=14)


def make_figure(report: dict) -> plt.Figure:
    fig, (ax_left, ax_right) = plt.subplots(
        1, 2, figsize=FIG_SIZE, gridspec_kw={"width_ratios": WIDTH_RATIOS, "wspace": WSPACE}
    )
    fig.patch.set_facecolor(SURFACE)
    plot_time_ranges(ax_left, report)
    plot_hours_by_difficulty(ax_right, report)
    return fig


def save_figure(fig: plt.Figure, plot_dir: Path) -> list[Path]:
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "pdf"):
        path = plot_dir / f"{PLOT_NAME}.{ext}"
        fig.savefig(path, dpi=SAVE_DPI, bbox_inches="tight", facecolor=SURFACE)
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--results", type=Path, default=None,
        help=f"task-analysis JSON to plot (default: newest in {RESULTS_DIR})",
    )
    parser.add_argument(
        "--plot-dir", type=Path, default=PLOT_DIR,
        help=f"directory the figure is saved to as {PLOT_NAME}.png/.pdf (default: {PLOT_DIR})",
    )
    args = parser.parse_args()

    results = args.results or newest_results()
    report = json.loads(results.read_text())
    print(f"read {results}  ({report['task_count']} tasks, generated {report['generated_at']})")

    fig = make_figure(report)
    for path in save_figure(fig, args.plot_dir):
        print(f"wrote {path}")
    plt.close(fig)


if __name__ == "__main__":
    main()
