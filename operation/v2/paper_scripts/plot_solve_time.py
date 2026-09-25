#!/usr/bin/env python
"""Plot the expected human solve time of the v2 task set.

Reads the task-analysis JSON written by plot_difficulty_and_types.py (newest
file in operation/results/v2/task_analysis/ unless --results is given) for each
task's expert estimate, case_classification.time_assumption_h. How agent time
tracks that estimate is the pace panel of plot_accuracy_by_difficulty.py.

Draws two figures under operation/results/v2/plots/solve_time/:

  solve_time.{png,pdf}
    Every task's expert estimate as a dot, grouped by
    tasks.human_difficulty_measure on a log axis, with the bucket median drawn
    as a bar and labelled. Each bucket's column is as wide as its share of the
    tasks (the x axis runs 0-100% of the set), so the dots sit at one density
    and the widths are the proportions. Pale rounded bands, tints of one blue
    (darker for the harder set), mark who a bucket is pitched at, Medium at a
    post-grad MBA and Medium-Hard and Hard at seasoned (3Y+) modelers, with
    their share of the set over them in the Hard blue, as is the longest-task
    callout. Half the text width. Shows the long tail and how much of the set
    is hard.

  solve_time_ranges.{png,pdf}
    Histogram of case_classification.time_range buckets, shortest to longest,
    each bar stacked by difficulty. Shows the right skew: most tasks sit under
    4 h and a thin tail runs past 20 h.

The canvases are the printed size (style_guide.yaml print.textwidth_in: one
\\textwidth for the ranges, half of it for solve_time), so the font sizes in the
FIGURE STYLE block are the sizes that print; include them at those widths and do
not scale them. Colours,
typeface, and the bar/legend helpers are shared with plot_difficulty_and_types.py
and plot_leaderboard.py in this directory. Run with the shared plotting
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

import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    AXIS,
    DIFFICULTY_COLORS,
    INK,
    RESULTS_DIR,
    STYLE,
    SURFACE,
    UNKNOWN_COLOR,
    _RoundedHandler,
    axis_title,
    quiet_axes,
    squircle_bar_ends,
    tick_top,
)
from plot_leaderboard import LEGEND_WEIGHT  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "solve_time"
PLOT_NAME = "solve_time"
RANGES_PLOT_NAME = "solve_time_ranges"


# ============================================================================
# FIGURE STYLE
# ============================================================================
# The canvas is the printed size (style_guide.yaml print.textwidth_in, included
# with width=\textwidth), so every font size below is the size that prints.
# fit_to_canvas pulls the axes in so the labels fit that width; the heights below
# are the axes', and the canvas grows by whatever hangs above and below them.
PRINT = STYLE["print"]
FIG_W_IN = PRINT["textwidth_in"]
FIG_SIZE = (FIG_W_IN / 2, 1.3)  # half the text width; the header above adds its own height
RANGES_FIG_SIZE = (FIG_W_IN, 1.9)
FIT_ITERS = 3  # fit_to_canvas passes; the spill settles after the second
SAVE_DPI = 300
SAVE_PAD_IN = PRINT["save_pad_in"]  # tight-bbox padding around the saved figure
RANGE_LABEL_ROTATION = 45  # degrees; the time_range strings are wide

TICK_FS = PRINT["pt"]["label"]  # range and difficulty names
VALUE_FS = PRINT["pt"]["label"]  # counts at bar tips, median labels
LEGEND_FS = PRINT["pt"]["label"]
LEGEND_HANDLE_LEN = 1.7  # legend swatch length, in font sizes

BAR_WIDTH = 0.72
VALUE_GAP = 0.02  # bar tip to its count, as a share of the tallest bar
HEADROOM = 1.12  # the y-limit clears the tallest bar by this factor, then rounds up to a tick
YTICK_STEP = 5  # tasks between y ticks
BAR_X_PAD = 0.6  # bars' worth of space before the first and after the last bar

DOT_SIZE = 9  # scatter marker area in points^2 (~3 pt across)
DOT_ALPHA = 0.85
DOT_EDGE_LW = 0.4
JITTER = 0.25  # half-width of the horizontal jitter, as a share of the column's width
MEDIAN_OVERHANG = 1  # % of the set the median bar runs past the dot cloud on each side
MEDIAN_LINE_PT = 1.4
MEDIAN_LABEL_GAP = 1  # % of the set between the median bar and its value
LOG_TICKS = (0.5, 1, 2, 5, 10, 20, 40)  # hours shown on the log axis; 40 closes it just above the 33 h task
JITTER_SEED = 0  # fixed so the figure is reproducible

ROW_EM = 1.2  # a text line's height, in font sizes
# the header above the axes, in points from the axes' top so it keeps its room at any height
SHARE_PT = 7  # the shares of the set, over the bands (and the unbanded columns)
BAND_CAPTION_PT = 24  # the bands' captions (bottom of their two lines)
BAND_TOP_PT = 50  # where the bands end, clear of the captions
BAND_HUE = DIFFICULTY_COLORS["Medium"]  # both bands are tints of this blue, so they read as one family; deeper blues tint to grey
BAND_ROUND_PT = 5  # band corner radius
BAND_GAP_PT = 3  # between neighbouring bands
HEADER_INK = DIFFICULTY_COLORS["Hard"]  # band captions, their shares and the longest-task callout
# (buckets, caption, tint): who each band's tasks are pitched at; tint = share of BAND_HUE over the
# surface, light enough that the grid still reads through
BANDS = (
    (("Medium",), "Post-Grad\nMBA", 0.07),
    (("Medium-Hard", "Hard"), "Seasoned (3Y+)\nmodelers", 0.13),
)
LONGEST_OFFSET_PT = (-10, 0)  # the longest-task callout, left of that task's dot
LONGEST_WEIGHT = "bold"  # the callout is the figure's one headline number
LEADER_LW = 0.6
SLATE = STYLE["ink"]["slate"]

plt.rcParams["axes.unicode_minus"] = False

def newest_results() -> Path:
    files = sorted(RESULTS_DIR.glob("*.json"))
    if not files:
        sys.exit(
            f"no task-analysis JSON under {RESULTS_DIR}; run "
            "plot_difficulty_and_types.py first or pass --results"
        )
    return files[-1]


def _log_ticks(ax, axis: str, candidates, lo: float, hi: float) -> None:
    ticks = [t for t in candidates if lo <= t <= hi]
    labels = [f"{t:g}" for t in ticks]
    if axis == "x":
        ax.set_xticks(ticks, labels)
        ax.xaxis.set_minor_locator(plt.NullLocator())
    else:
        ax.set_yticks(ticks, labels)
        ax.yaxis.set_minor_locator(plt.NullLocator())


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
            x, counts, bottom=bottom, width=BAR_WIDTH, label=d,
            color=DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR),
            edgecolor=SURFACE, linewidth=0.6, zorder=2,
        )
        handles.append(bars[0])
        for i, (rect, c) in enumerate(zip(bars, counts)):
            if c > 0:
                outer[i] = rect
        bottom = bottom + counts
    tallest = bottom.max()
    for xi, total in zip(x, bottom):
        ax.text(
            xi, total + tallest * VALUE_GAP, str(int(total)),
            ha="center", va="bottom", fontsize=VALUE_FS, color=INK,
        )
    quiet_axes(ax)
    ax.set_xticks(
        x, ranges, fontsize=TICK_FS, color=INK,
        rotation=RANGE_LABEL_ROTATION, ha="right", rotation_mode="anchor",
    )
    ax.set_xlim(-BAR_X_PAD, len(ranges) - 1 + BAR_X_PAD)
    top, _ = tick_top(tallest * HEADROOM, YTICK_STEP)
    ax.set_ylim(0, top)
    ax.set_yticks(range(0, top + 1, YTICK_STEP))
    axis_title(ax, "Tasks")
    axis_title(ax, "Expected solve time", axis="x")
    ax.legend(
        handles=handles, labels=difficulties, loc="upper right",
        borderpad=0, borderaxespad=0, handlelength=LEGEND_HANDLE_LEN,
        handletextpad=0.4, labelspacing=0.3,
        prop={"size": LEGEND_FS, "weight": LEGEND_WEIGHT}, frameon=False, labelcolor=INK,
        handler_map={mpatches.Rectangle: _RoundedHandler()},
    )
    squircle_bar_ends(ax, [r for r in outer if r is not None], horizontal=False)


def plot_hours_by_difficulty(ax, report: dict) -> list[tuple[float, float, str]]:
    """The dots, medians and header; returns the bands for draw_bands once the layout is final."""
    difficulties = list(report["difficulty"])
    tasks = report["tasks"]
    rng = np.random.default_rng(JITTER_SEED)
    spans = column_spans(difficulties, tasks)
    all_hours = []
    for d in difficulties:
        hours = np.array([t["time_h"] for t in tasks if t["difficulty"] == d])
        all_hours += hours.tolist()
        color = DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR)
        x0, x1 = spans[d]
        i = (x0 + x1) / 2
        xs = i + rng.uniform(-JITTER, JITTER, len(hours)) * (x1 - x0)
        ax.scatter(
            xs, hours, s=DOT_SIZE, color=color, alpha=DOT_ALPHA,
            edgecolor=SURFACE, linewidth=DOT_EDGE_LW, zorder=3,
        )
        med = median(hours)
        half = JITTER * (x1 - x0) + MEDIAN_OVERHANG  # across the whole cloud, so the label clears it
        ax.plot(
            [i - half, i + half], [med, med],
            color=INK, linewidth=MEDIAN_LINE_PT, solid_capstyle="round", zorder=4,
        )
        ax.text(
            i + half + MEDIAN_LABEL_GAP, med, f"{med:.1f}",
            ha="left", va="center_baseline", fontsize=VALUE_FS, color=INK, zorder=4,
        )
    quiet_axes(ax)
    ax.set_yscale("log")
    # snap to the ticks just outside the data, so the grid closes the panel at both ends
    lo = max(t for t in LOG_TICKS if t <= min(all_hours))
    hi = min(t for t in LOG_TICKS if t >= max(all_hours))
    ax.set_ylim(lo, hi)
    _log_ticks(ax, "y", LOG_TICKS, lo, hi)
    # 'Medium-Hard' -> 'Medium-\nHard' so the four names fit side by side
    ax.set_xticks([sum(spans[d]) / 2 for d in difficulties], [d.replace("-", "-\n") for d in difficulties],
                  fontsize=TICK_FS, color=INK, fontweight=LEGEND_WEIGHT)
    # centre each name on the two-line one's middle rather than hanging all from the top
    lines = max(d.count("-") + 1 for d in difficulties)
    for label in ax.get_xticklabels():
        label.set_verticalalignment("center")
    ax.tick_params(axis="x", pad=AXIS["tick_pad_pt"] + TICK_FS * ROW_EM * lines / 2)
    ax.set_xlim(0, 100)
    axis_title(ax, "Expected solve time (h)")
    return annotate_shares(ax, difficulties, tasks, spans)


def column_spans(difficulties: list[str], tasks: list[dict]) -> dict[str, tuple[float, float]]:
    """Each bucket's (left, right) on the x axis, in % of the set, side by side from 0."""
    spans, left = {}, 0.0
    for d in difficulties:
        width = 100 * sum(1 for t in tasks if t["difficulty"] == d) / len(tasks)
        spans[d] = (left, left + width)
        left += width
    return spans


def annotate_shares(ax, difficulties: list[str], tasks: list[dict],
                    spans: dict[str, tuple[float, float]]) -> list[tuple[float, float, str]]:
    """Caption and share of the set over each BANDS entry, the unbanded buckets' shares in
    slate, and the longest task; returns each band's (left, right, fill) for draw_bands."""
    over = ax.get_xaxis_transform()  # x in data, y in axes fraction

    def share(x0: float, x1: float, color: str) -> None:
        ax.annotate(f"{x1 - x0:.0f}%", xy=((x0 + x1) / 2, 1), xycoords=over, xytext=(0, SHARE_PT),
                    textcoords="offset points", ha="center", va="bottom", annotation_clip=False,
                    fontsize=VALUE_FS, color=color, fontweight=LEGEND_WEIGHT)

    banded, bands = set(), []
    for buckets, caption, tint in BANDS:
        present = [d for d in buckets if d in spans]
        if not present:
            continue
        banded.update(present)
        x0, x1 = min(spans[d][0] for d in present), max(spans[d][1] for d in present)
        fill = tint * np.array(mcolors.to_rgb(BAND_HUE)) + (1 - tint) * np.array(mcolors.to_rgb(SURFACE))
        bands.append((x0, x1, mcolors.to_hex(fill)))
        ax.annotate(caption, xy=((x0 + x1) / 2, 1), xycoords=over, xytext=(0, BAND_CAPTION_PT),
                    textcoords="offset points", ha="center", va="bottom", annotation_clip=False,
                    fontsize=VALUE_FS, color=HEADER_INK, fontweight=LEGEND_WEIGHT, linespacing=1.1)
        share(x0, x1, HEADER_INK)
    for d in difficulties:
        if d not in banded:
            share(*spans[d], SLATE)
    longest = max(tasks, key=lambda t: t["time_h"])
    ink = HEADER_INK
    ax.annotate(f"Longest: {longest['time_h']:.0f} h",
                xy=(sum(spans[longest["difficulty"]]) / 2, longest["time_h"]),
                xytext=LONGEST_OFFSET_PT, textcoords="offset points", ha="right", va="center_baseline",
                fontsize=VALUE_FS, color=ink, fontweight=LONGEST_WEIGHT,
                arrowprops=dict(arrowstyle="-", color=ink, lw=LEADER_LW, shrinkA=1, shrinkB=3))
    return bands


def draw_bands(ax, bands: list[tuple[float, float, str]]) -> None:
    """Rounded bands from the baseline to BAND_TOP_PT above the axes, behind the grid.

    Drawn after fit_to_canvas: the corner radius and the gap are in points, converted with
    the axes' final size, so the corners come out round rather than stretched.
    """
    fig = ax.figure
    pos = ax.get_position()
    w_in, h_in = pos.width * fig.get_figwidth(), pos.height * fig.get_figheight()
    x_lo, x_hi = ax.get_xlim()
    x_per_pt = (x_hi - x_lo) / (w_in * 72)
    y_per_pt = 1 / (h_in * 72)  # y in axes fraction
    over = ax.get_xaxis_transform()
    radius = BAND_ROUND_PT * x_per_pt
    for x0, x1, fill in bands:
        left = x0 + (BAND_GAP_PT / 2 * x_per_pt if x0 > x_lo else 0)
        right = x1 - (BAND_GAP_PT / 2 * x_per_pt if x1 < x_hi else 0)
        ax.add_patch(mpatches.FancyBboxPatch(
            (left, 0), right - left, 1 + BAND_TOP_PT * y_per_pt,
            boxstyle=f"round,pad=0,rounding_size={radius}", mutation_aspect=y_per_pt / x_per_pt,
            transform=over, facecolor=fill, edgecolor="none", zorder=0.1, clip_on=False,
        ))

def fit_to_canvas(fig: plt.Figure, iters: int = FIT_ITERS) -> None:
    """Move the gridspec so labels, titles and legends land inside the printed width.

    The axes start at the canvas edges and everything outside them spills over, which
    bbox_inches="tight" would keep, printing wider than \\textwidth. Pull the axes in
    by the measured spill on each side until the drawn extent is exactly FIG_W_IN
    wide; the axes keep their height and the canvas grows to hold what hangs below
    and above them.
    """
    gs = fig.axes[0].get_gridspec()
    w, h = fig.get_size_inches()
    ax_h = (gs.top - gs.bottom) * h
    for _ in range(iters):
        fig.canvas.draw()
        bb = fig.get_tightbbox(fig.canvas.get_renderer())  # inches
        left, right = gs.left * w - bb.x0, gs.right * w - (bb.x1 - w)
        below, above = gs.bottom * h - bb.y0, bb.y1 - gs.top * h
        h = below + ax_h + above
        fig.set_size_inches(w, h)
        gs.update(left=left / w, right=right / w, bottom=below / h, top=(below + ax_h) / h)


def make_figure(report: dict) -> plt.Figure:
    fig, ax = plt.subplots(
        figsize=FIG_SIZE,
        gridspec_kw={"left": 0.0, "right": 1.0, "top": 1.0, "bottom": 0.0},
    )
    fig.patch.set_facecolor(SURFACE)
    bands = plot_hours_by_difficulty(ax, report)
    fit_to_canvas(fig)
    draw_bands(ax, bands)
    return fig


def make_ranges_figure(report: dict) -> plt.Figure:
    fig, ax = plt.subplots(
        figsize=RANGES_FIG_SIZE,
        gridspec_kw={"left": 0.0, "right": 1.0, "top": 1.0, "bottom": 0.0},
    )
    fig.patch.set_facecolor(SURFACE)
    plot_time_ranges(ax, report)
    fit_to_canvas(fig)
    return fig


def save_figure(fig: plt.Figure, plot_dir: Path, name: str) -> list[Path]:
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "pdf"):
        path = plot_dir / f"{name}.{ext}"
        fig.savefig(
            path, dpi=SAVE_DPI, bbox_inches="tight", pad_inches=SAVE_PAD_IN, facecolor=SURFACE,
        )
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
        help=f"directory the figures are saved to as {PLOT_NAME} and {RANGES_PLOT_NAME} .png/.pdf"
             f" (default: {PLOT_DIR})",
    )
    args = parser.parse_args()

    results = args.results or newest_results()
    report = json.loads(results.read_text())
    print(f"read {results}  ({report['task_count']} tasks, generated {report['generated_at']})")

    for fig, name in ((make_figure(report), PLOT_NAME),
                      (make_ranges_figure(report), RANGES_PLOT_NAME)):
        for path in save_figure(fig, args.plot_dir, name):
            print(f"wrote {path}")
        plt.close(fig)


if __name__ == "__main__":
    main()
