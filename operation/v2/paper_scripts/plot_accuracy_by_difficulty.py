#!/usr/bin/env python
"""Accuracy leaderboard on the harder tasks, and accuracy and time against difficulty.

Reads the live v2 database (config database.v2_url) the same way
plot_leaderboard.py does: for each (agent, task) the newest clean grading by the
current judge, but takes the Accuracy category alone,
scored_results.criteria_scores.Accuracy.normalized_score (0-100), instead of
the composite, along with the attempt's task_attempts.time_taken_min. Tasks
are bucketed by tasks.human_difficulty_measure.

Draws accuracy_by_difficulty.{png,pdf} under
operation/results/v2/plots/accuracy_by_difficulty/:

  Left          accuracy leaderboard over the selected difficulties
                (--difficulty, default Medium and harder): one column per
                cohort, best at the left, mean accuracy with a standard-error
                whisker, the score on the cap and "n/N" in muted ink when the
                cohort has not covered every selected task. Columns carry the
                leaderboard's encoding: vendor hue, harness tint and hatch,
                squircle cap. A cohort graded on fewer than --min-tasks
                selected tasks is left out.
  Right, top    mean accuracy of the selected cohorts (--models, default the
                top --top of the left panel) at each difficulty bucket, easiest
                to hardest: a PCHIP-smoothed line through the bucket means with
                a translucent standard-error ribbon, a harness marker at each
                bucket (GUI circle, Excel square, Code diamond, In-house
                triangle) and the cohorts named in a legend whose handles
                repeat the line and marker.
  Right, bottom the same cohorts' mean completion time per bucket, minutes on
                a log axis, drawn the same way.

Cohorts are named in plot_leaderboard.AGENTS; --models takes their display
names or agent_model_name keys, case-insensitively. Run with the shared plotting
environment, ~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --top 4 --no-smooth
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --models "Claude Code (Fable 5.1)" "Codex (GPT-6 Astra)"
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --difficulty Medium-Hard Hard --min-tasks 20
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --time-scale linear
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --database-url postgresql://...
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib.colors as mcolors
import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import psycopg2
import psycopg2.extras
from matplotlib.legend_handler import HandlerTuple
from scipy.interpolate import PchipInterpolator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    BASELINE,
    DIFFICULTY_ORDER,
    GRID,
    INK,
    MUTED,
    REPO_ROOT,
    SURFACE,
    UNKNOWN_COLOR,
    database_url,
    order_key,
)
from plot_leaderboard import (  # noqa: E402
    AGENTS,
    COL_IN,
    COMPANY_COLORS,
    HARNESS,
    HARNESS_HATCH,
    HARNESS_ORDER,
    HATCH_ALPHA,
    LEGEND_HANDLE_LENGTH,
    LEGEND_HARNESS_FACE,
    MIN_JUDGE_VERSION,
    NAME_ROTATION,
    SE_CAP_PT,
    SE_LW,
    SLATE,
    VALUE_PAD,
    YLIM,
    YTICKS,
    _HatchedHandler,
    _tinted,
    save_figure,
    squircle_column,
)

PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "accuracy_by_difficulty"
PLOT_NAME = "accuracy_by_difficulty"
DIFFICULTY_DEFAULT = ("Medium", "Medium-Hard", "Hard")  # the leaderboard's task set
MIN_TASKS_DEFAULT = 25  # cohorts graded on fewer selected tasks are still in flight
TOP_DEFAULT = 6  # lines on the right when --models is not given

# ============================================================================
# FIGURE STYLE
# ============================================================================
MIN_LEFT_W_IN = 7.0  # the leaderboard panel widens with the roster, from here
RIGHT_W_IN = 7.6
RIGHT_PANEL_H_IN = 3.7  # each of the two stacked right panels
ROW_GAP_IN = 0.55  # between the accuracy and time panels
AXES_H_IN = 2 * RIGHT_PANEL_H_IN + ROW_GAP_IN  # the leaderboard spans both rows
LEFT_IN = 1.05
GAP_IN = 1.5  # between the columns; the right panels have their own y ticks
RIGHT_IN = 0.3
TOP_IN = 0.7  # panel titles
BOTTOM_IN = 2.4  # tilted names on the left panel
SAVE_DPI = 200

# Type is a step larger than the leaderboard's: the figure is wider and reads at page width.
TITLE_FS = 18
NAME_FS = 16
VALUE_FS = 16
COVER_FS = 12
TICK_FS = 15
AXIS_FS = 17
LEGEND_FS = 17  # the vendor and harness keys inside the leaderboard
AXIS_TITLE_COLOR = SLATE  # style_guide ink.slate: secondary text, a shade firmer than the muted tick grey

# vendor and harness legends stack at the upper right of the leaderboard, above its shortest columns
KEY_LEGEND_X = 1.0  # axes fraction of the legends' right edge
KEY_LEGEND_Y = (1.0, 0.895)  # axes fraction of the vendor and harness legends' top edges

LINE_LW = 3.0
LINE_ALPHA = 0.9
BAND_ALPHA = 0.16
MARKER_SIZE = 11
MARKER_EDGE_LW = 1.6
HARNESS_MARKER = {"GUI": "o", "Excel": "s", "Code": "D", "In-house": "^"}
X_PAD = 0.3  # x units beyond the first and last bucket
ACCURACY_YLIM = (0, 105)  # headroom so a marker at 100 is not clipped by the axes edge
SMOOTH_SAMPLES = 200  # interpolated points per line under --smooth
TIME_TICK_CANDIDATES = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000)
TIME_LOG_HEADROOM = 1.25  # y-limit factor above the highest band edge on a log axis
TIME_LINEAR_HEADROOM = 1.1

# cohort legend inside the accuracy panel; the lines fall to the right, so the lower left is empty
COHORT_LEGEND_LOC = "lower left"
COHORT_LEGEND_NCOL = 2
COHORT_LEGEND_FS = 16
COHORT_HANDLE_LENGTH = 2.6  # line with the marker at its middle

SQL = """
    select distinct on (ta.agent_model_name, ta.task_id)
           ta.agent_model_name, ta.agent_model_type, ta.task_id, ta.time_taken_min,
           t.human_difficulty_measure as difficulty,
           (g.scored_results->'criteria_scores'->'Accuracy'->>'normalized_score')::float as accuracy
    from gradings g
    join task_attempts ta on ta.id = g.attempt_id
    join tasks t on t.id = ta.task_id
    where g.judge_version >= %(judge)s
      and g.failed is not true and g.deprecated is not true
      and ta.deprecated is not true and t.deprecated is not true
      and g.scored_results->>'total_score' is not null
    order by ta.agent_model_name, ta.task_id, g.created_at desc
"""


@dataclass
class Cohort:
    key: str
    name: str
    company: str
    harness: str
    scores: dict[str, list[float]] = field(default_factory=dict)  # difficulty -> accuracies
    minutes: dict[str, list[float]] = field(default_factory=dict)  # difficulty -> completion times

    def over(self, difficulties) -> list[float]:
        return [s for d in difficulties for s in self.scores.get(d, [])]

    @property
    def color(self) -> str:
        """Vendor hue, mixed toward the surface by the harness tint."""
        return _tinted(COMPANY_COLORS.get(self.company, UNKNOWN_COLOR), self.harness)


def mean_se(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return float("nan"), 0.0
    se = float(np.std(values, ddof=1) / np.sqrt(n)) if n > 1 else 0.0
    return float(np.mean(values)), se


# --- data ----------------------------------------------------------------------


def fetch(conn) -> tuple[dict[str, Cohort], dict[str, int]]:
    """Every registered cohort with its per-difficulty accuracies and times, and live tasks per difficulty."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select human_difficulty_measure as d, count(*) as n from tasks"
                " where deprecated is not true group by 1")
    tasks_per_difficulty = {r["d"]: r["n"] for r in cur.fetchall()}
    cur.execute(SQL, {"judge": MIN_JUDGE_VERSION})
    rows = cur.fetchall()

    by_key: dict[str, Cohort] = {}
    unknown: dict[str, int] = {}
    no_accuracy = no_time = 0
    for r in rows:
        key = r["agent_model_name"]
        if key not in AGENTS:
            unknown[key] = unknown.get(key, 0) + 1
            continue
        if r["accuracy"] is None:
            no_accuracy += 1
            continue
        if key not in by_key:
            name, company = AGENTS[key]
            by_key[key] = Cohort(key, name, company, HARNESS.get(r["agent_model_type"], "?"))
        c = by_key[key]
        c.scores.setdefault(r["difficulty"], []).append(r["accuracy"])
        if r["time_taken_min"] is not None and r["time_taken_min"] > 0:
            c.minutes.setdefault(r["difficulty"], []).append(float(r["time_taken_min"]))
        else:
            no_time += 1

    for key, n in sorted(unknown.items()):
        print(f"skipping {key}: {n} graded tasks but not in AGENTS", file=sys.stderr)
    if no_accuracy:
        print(f"skipping {no_accuracy} gradings with no Accuracy category score", file=sys.stderr)
    if no_time:
        print(f"{no_time} graded attempts have no positive time_taken_min; left out of the time panel",
              file=sys.stderr)
    for c in by_key.values():
        if c.company not in COMPANY_COLORS:
            print(f"{c.name}: vendor {c.company!r} has no colour in style_guide.yaml", file=sys.stderr)
    return by_key, tasks_per_difficulty


def leaderboard(cohorts: dict[str, Cohort], difficulties: tuple[str, ...], min_tasks: int) -> list[Cohort]:
    kept = []
    for c in cohorts.values():
        n = len(c.over(difficulties))
        if n < min_tasks:
            print(f"skipping {c.name}: {n} < {min_tasks} graded tasks on {', '.join(difficulties)}",
                  file=sys.stderr)
            continue
        kept.append(c)
    return sorted(kept, key=lambda c: -mean_se(c.over(difficulties))[0])


def select_models(cohorts: dict[str, Cohort], ranked: list[Cohort], names: list[str] | None, top: int) -> list[Cohort]:
    """--models by display name or key (case-insensitive), else the top of the leaderboard."""
    if not names:
        return ranked[:top]
    lookup = {c.name.lower(): c for c in cohorts.values()}
    lookup.update({c.key.lower(): c for c in cohorts.values()})
    chosen = []
    for name in names:
        c = lookup.get(name.lower())
        if c is None:
            sys.exit(f"--models: {name!r} is not a graded cohort; see plot_leaderboard.AGENTS")
        if c not in chosen:
            chosen.append(c)
    return chosen


# --- figure ----------------------------------------------------------------------


def style_axis(ax) -> None:
    """Recessive chrome: baseline only, hairline grid on the value axis."""
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0, pad=6)


def plot_leaderboard_panel(ax, ranked: list[Cohort], difficulties: tuple[str, ...], n_tasks: int) -> None:
    xs = list(range(len(ranked)))
    ax.set_xlim(-0.6, xs[-1] + 0.6)
    ax.set_ylim(*YLIM)
    style_axis(ax)
    ax.set_yticks(YTICKS, [str(t) for t in YTICKS], fontsize=TICK_FS, color=MUTED)
    ax.set_ylabel("Accuracy (0-100)", fontsize=AXIS_FS, color=AXIS_TITLE_COLOR, labelpad=10)
    ax.set_xticks(xs, [c.name for c in ranked], fontsize=NAME_FS, color=INK,
                  rotation=NAME_ROTATION, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="x", length=0, pad=8)
    ax.set_title(f"Accuracy on {_difficulty_phrase(difficulties)} tasks",
                 fontsize=TITLE_FS, color=INK, pad=12)

    hatch_color = mcolors.to_rgba(SURFACE, HATCH_ALPHA)
    for x, c in zip(xs, ranked):
        scores = c.over(difficulties)
        m, se = mean_se(scores)
        squircle_column(ax, x, m, facecolor=c.color, edgecolor=hatch_color, linewidth=0,
                        hatch=HARNESS_HATCH.get(c.harness, ""), zorder=2)
        if se > 0:
            ax.errorbar(x, m, yerr=se, fmt="none", ecolor=INK, elinewidth=SE_LW,
                        capsize=SE_CAP_PT, capthick=SE_LW, zorder=4)
        t = ax.text(x, m + se + VALUE_PAD, f"{m:.1f}", ha="center", va="bottom",
                    fontsize=VALUE_FS, color=INK, zorder=4)
        if len(scores) < n_tasks:
            ax.annotate(f"{len(scores)}/{n_tasks}", xy=(0.5, 1), xycoords=t, xytext=(0, 2),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=COVER_FS, color=SLATE)


def _smooth(xs: list[float], ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shape-preserving (PCHIP) curve through the knots: no overshoot past the data."""
    xa, ya = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    x_dense = np.linspace(xa[0], xa[-1], SMOOTH_SAMPLES)
    if xa.size == 2:
        return x_dense, np.interp(x_dense, xa, ya)
    return x_dense, PchipInterpolator(xa, ya)(x_dense)


def draw_lines(ax, selected: list[Cohort], buckets: list[str], values_of, smooth: bool,
               floor: float | None = None) -> tuple[list[float], list[float]]:
    """One smoothed mean line with a standard-error ribbon per cohort.

    `values_of(cohort, bucket)` gives the samples; a bucket without any leaves a gap.
    `floor` clips the ribbon's lower edge (a log axis cannot show zero).
    Returns every ribbon's low and high edges for the caller's y limits.
    """
    lows, highs = [], []
    for c in selected:
        xs, means, ses = [], [], []
        for x, d in enumerate(buckets):
            vals = values_of(c, d)
            if vals:
                m, se = mean_se(vals)
                xs.append(x)
                means.append(m)
                ses.append(se)
        if not xs:
            continue
        means, ses = np.asarray(means), np.asarray(ses)
        lo, hi = means - ses, means + ses
        if floor is not None:
            lo = np.maximum(lo, floor)
        lows += lo.tolist()
        highs += hi.tolist()
        if smooth and len(xs) >= 2:
            x_line, y_line = _smooth(xs, means)
            _, y_lo = _smooth(xs, lo)
            _, y_hi = _smooth(xs, hi)
        else:
            x_line, y_line, y_lo, y_hi = np.asarray(xs, dtype=float), means, lo, hi
        ax.fill_between(x_line, y_lo, y_hi, color=c.color, alpha=BAND_ALPHA, linewidth=0, zorder=1)
        ax.plot(x_line, y_line, color=c.color, lw=LINE_LW, alpha=LINE_ALPHA, solid_capstyle="round",
                solid_joinstyle="round", zorder=2)
        ax.plot(xs, means, linestyle="none", marker=HARNESS_MARKER.get(c.harness, "o"), ms=MARKER_SIZE,
                mfc=c.color, mec=SURFACE, mew=MARKER_EDGE_LW, zorder=3)
    ax.set_xlim(-X_PAD, len(buckets) - 1 + X_PAD)
    ax.set_xticks(range(len(buckets)), buckets, fontsize=TICK_FS, color=INK)
    ax.tick_params(axis="x", length=0, pad=8)
    return lows, highs


def plot_accuracy_panel(ax, selected: list[Cohort], buckets: list[str], smooth: bool) -> None:
    style_axis(ax)
    draw_lines(ax, selected, buckets, lambda c, d: c.scores.get(d, []), smooth)
    ax.set_ylim(*ACCURACY_YLIM)
    ax.set_yticks(YTICKS, [str(t) for t in YTICKS], fontsize=TICK_FS, color=MUTED)
    ax.set_ylabel("Accuracy (0-100)", fontsize=AXIS_FS, color=AXIS_TITLE_COLOR, labelpad=10)
    ax.tick_params(axis="x", labelbottom=False)  # the time panel below carries the bucket names
    ax.set_title("Accuracy as difficulty increases", fontsize=TITLE_FS, color=INK, pad=12)

    # each entry is the series as drawn: the line in the cohort colour with its harness marker
    handles = [mlines.Line2D([], [], color=c.color, lw=LINE_LW, alpha=LINE_ALPHA, solid_capstyle="round",
                             marker=HARNESS_MARKER.get(c.harness, "o"), ms=MARKER_SIZE, mfc=c.color,
                             mec=SURFACE, mew=MARKER_EDGE_LW, label=c.name) for c in selected]
    ax.legend(handles=handles, loc=COHORT_LEGEND_LOC, ncol=COHORT_LEGEND_NCOL, fontsize=COHORT_LEGEND_FS,
              labelcolor=INK, frameon=True, facecolor=SURFACE, edgecolor="none", framealpha=0.85,
              handlelength=COHORT_HANDLE_LENGTH, handletextpad=0.6, markerscale=1.0,
              columnspacing=1.2, labelspacing=0.45, borderpad=0.5)


def plot_time_panel(ax, selected: list[Cohort], buckets: list[str], smooth: bool, log: bool) -> None:
    style_axis(ax)
    if log:
        ax.set_yscale("log")
    positive = [m for c in selected for d in buckets for m in c.minutes.get(d, [])]
    floor = min(positive) / 2 if (log and positive) else None
    lows, highs = draw_lines(ax, selected, buckets, lambda c, d: c.minutes.get(d, []), smooth, floor=floor)
    if highs:
        if log:
            lo, hi = min(lows) / TIME_LOG_HEADROOM, max(highs) * TIME_LOG_HEADROOM
        else:
            lo, hi = 0.0, max(highs) * TIME_LINEAR_HEADROOM
        ax.set_ylim(lo, hi)
        if log:
            ticks = [t for t in TIME_TICK_CANDIDATES if lo <= t <= hi]
            ax.set_yticks(ticks, [str(t) for t in ticks])
            ax.yaxis.set_minor_locator(plt.NullLocator())
    ax.tick_params(axis="y", labelsize=TICK_FS, labelcolor=MUTED)
    ax.set_ylabel("Completion time (min)", fontsize=AXIS_FS, color=AXIS_TITLE_COLOR, labelpad=10)
    ax.set_xlabel("Task difficulty", fontsize=AXIS_FS, color=AXIS_TITLE_COLOR, labelpad=10)
    ax.set_title("Completion time as difficulty increases", fontsize=TITLE_FS, color=INK, pad=12)


def make_figure(ranked: list[Cohort], selected: list[Cohort], difficulties: tuple[str, ...],
                buckets: list[str], n_tasks: int, smooth: bool, log_time: bool) -> plt.Figure:
    left_w_in = max(MIN_LEFT_W_IN, len(ranked) * COL_IN)
    fig_w = LEFT_IN + left_w_in + GAP_IN + RIGHT_W_IN + RIGHT_IN
    fig_h = TOP_IN + AXES_H_IN + BOTTOM_IN
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=SURFACE)
    ax_l = fig.add_axes([LEFT_IN / fig_w, BOTTOM_IN / fig_h, left_w_in / fig_w, AXES_H_IN / fig_h])
    right_x = (LEFT_IN + left_w_in + GAP_IN) / fig_w
    ax_top = fig.add_axes([right_x, (BOTTOM_IN + RIGHT_PANEL_H_IN + ROW_GAP_IN) / fig_h,
                           RIGHT_W_IN / fig_w, RIGHT_PANEL_H_IN / fig_h])
    ax_bot = fig.add_axes([right_x, BOTTOM_IN / fig_h, RIGHT_W_IN / fig_w, RIGHT_PANEL_H_IN / fig_h])
    plot_leaderboard_panel(ax_l, ranked, difficulties, n_tasks)
    plot_accuracy_panel(ax_top, selected, buckets, smooth)
    plot_time_panel(ax_bot, selected, buckets, smooth, log_time)

    # vendor legend, then the harness legend (hatch and marker), stacked at the leaderboard's upper right
    everyone = ranked + [c for c in selected if c not in ranked]
    vendors = []
    for c in everyone:
        if c.company not in vendors:
            vendors.append(c.company)
    hatch_color = mcolors.to_rgba(SURFACE, HATCH_ALPHA)
    v_handles = [mpatches.Patch(facecolor=COMPANY_COLORS.get(v, UNKNOWN_COLOR), edgecolor="none", label=v)
                 for v in vendors]
    harnesses = [h for h in HARNESS_ORDER if any(c.harness == h for c in everyone)]
    h_handles = [
        (mpatches.Patch(facecolor=_tinted(LEGEND_HARNESS_FACE, h), edgecolor=hatch_color, hatch=HARNESS_HATCH[h]),
         mlines.Line2D([], [], color=_tinted(LEGEND_HARNESS_FACE, h), marker=HARNESS_MARKER[h], ms=MARKER_SIZE,
                       mec=SURFACE, mew=MARKER_EDGE_LW, lw=0))
        for h in harnesses
    ]
    common = dict(frameon=False, fontsize=LEGEND_FS, labelcolor=INK, handleheight=1.0,
                  columnspacing=1.4, handletextpad=0.5, borderpad=0, borderaxespad=0, alignment="right",
                  handler_map={mpatches.Patch: _HatchedHandler(), tuple: HandlerTuple(ndivide=None, pad=0.4)})
    vendor_legend = ax_l.legend(handles=v_handles, loc="upper right", ncol=len(v_handles), title="Model vendor",
                                title_fontsize=LEGEND_FS, handlelength=LEGEND_HANDLE_LENGTH,
                                bbox_to_anchor=(KEY_LEGEND_X, KEY_LEGEND_Y[0]), **common)
    ax_l.add_artist(vendor_legend)  # a second ax.legend call would replace it
    ax_l.legend(handles=h_handles, labels=harnesses, loc="upper right", ncol=len(h_handles), title="Harness",
                title_fontsize=LEGEND_FS, handlelength=LEGEND_HANDLE_LENGTH + 1.4,
                bbox_to_anchor=(KEY_LEGEND_X, KEY_LEGEND_Y[1]), **common)
    return fig


def _difficulty_phrase(difficulties: tuple[str, ...]) -> str:
    """'Medium and harder' when the selection is a tail of the ladder, else the list."""
    ordered = sorted(difficulties, key=order_key(DIFFICULTY_ORDER))
    tail = DIFFICULTY_ORDER[DIFFICULTY_ORDER.index(ordered[0]):] if ordered[0] in DIFFICULTY_ORDER else ()
    if tuple(ordered) == tail and len(ordered) > 1:
        return f"{ordered[0]} and harder"
    return ", ".join(ordered)


# --- report ----------------------------------------------------------------------


def report(ranked: list[Cohort], selected: list[Cohort], difficulties: tuple[str, ...],
           buckets: list[str], tasks_per_difficulty: dict[str, int]) -> None:
    n_tasks = sum(tasks_per_difficulty.get(d, 0) for d in difficulties)
    print(f"{len(ranked)} cohorts on {', '.join(difficulties)} ({n_tasks} live tasks), "
          f"judge >= v{MIN_JUDGE_VERSION}, Accuracy category only")
    print(f"{'#':>3}  {'cohort':<34}{'harness':<10}{'vendor':<11}{'n':>4}{'mean':>7}{'se':>6}")
    for i, c in enumerate(ranked, 1):
        scores = c.over(difficulties)
        m, se = mean_se(scores)
        print(f"{i:>3}  {c.name:<34}{c.harness:<10}{c.company:<11}{len(scores):>4}{m:>7.1f}{se:>6.1f}")

    live = ", ".join(f"{d} {tasks_per_difficulty.get(d, 0)}" for d in buckets)
    for title, attr in (("accuracy", "scores"), ("completion time, minutes", "minutes")):
        print(f"\n{title} by difficulty, mean ± se (n); live tasks: {live}")
        print(f"{'cohort':<34}" + "".join(f"{d:>20}" for d in buckets))
        for c in selected:
            cells = []
            for d in buckets:
                vals = getattr(c, attr).get(d, [])
                m, se = mean_se(vals)
                cells.append(f"{m:.1f} ± {se:.1f} ({len(vals)})" if vals else "-")
            print(f"{c.name:<34}" + "".join(f"{cell:>20}" for cell in cells))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", help="full connection string; bypasses config/config.yaml")
    parser.add_argument("--difficulty", nargs="+", default=list(DIFFICULTY_DEFAULT), metavar="BUCKET",
                        help=f"difficulty buckets the leaderboard ranks on (default: {' '.join(DIFFICULTY_DEFAULT)})")
    parser.add_argument("--min-tasks", type=int, default=MIN_TASKS_DEFAULT,
                        help=f"drop cohorts graded on fewer selected tasks (default {MIN_TASKS_DEFAULT})")
    parser.add_argument("--models", nargs="+", metavar="NAME",
                        help="cohorts on the right panels, by display name or agent_model_name (default: --top of the leaderboard)")
    parser.add_argument("--top", type=int, default=TOP_DEFAULT,
                        help=f"how many leaderboard cohorts the right panels follow when --models is not given (default {TOP_DEFAULT})")
    parser.add_argument("--no-smooth", dest="smooth", action="store_false",
                        help="straight segments between buckets instead of the PCHIP curve")
    parser.add_argument("--time-scale", choices=["log", "linear"], default="log",
                        help="y scale of the completion-time panel (default log)")
    parser.add_argument("--plot-dir", type=Path, default=PLOT_DIR,
                        help=f"directory the figure is saved to as {PLOT_NAME}.png/.pdf (default: {PLOT_DIR})")
    args = parser.parse_args()

    difficulties = tuple(args.difficulty)
    unknown = [d for d in difficulties if d not in DIFFICULTY_ORDER]
    if unknown:
        sys.exit(f"--difficulty: unknown bucket(s) {unknown}; choose from {DIFFICULTY_ORDER}")

    conn = psycopg2.connect(database_url(args))
    try:
        cohorts, tasks_per_difficulty = fetch(conn)
    finally:
        conn.close()
    ranked = leaderboard(cohorts, difficulties, args.min_tasks)
    if not ranked:
        sys.exit("no cohort passes --min-tasks on the selected difficulties")
    selected = select_models(cohorts, ranked, args.models, args.top)
    buckets = sorted((d for d in tasks_per_difficulty if d in DIFFICULTY_ORDER), key=order_key(DIFFICULTY_ORDER))
    report(ranked, selected, difficulties, buckets, tasks_per_difficulty)

    n_tasks = sum(tasks_per_difficulty.get(d, 0) for d in difficulties)
    fig = make_figure(ranked, selected, difficulties, buckets, n_tasks, args.smooth, args.time_scale == "log")
    for path in save_figure(fig, args.plot_dir, PLOT_NAME):
        print(f"wrote {path}")
    plt.close(fig)


if __name__ == "__main__":
    main()
