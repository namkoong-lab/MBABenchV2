#!/usr/bin/env python
"""Final-answer accuracy leaderboard on the harder tasks, accuracy and time against difficulty, and agent time against the expert's.

Reads the live v2 database (config database.v2_url) the same way
plot_leaderboard.py does: for each (agent, task) the newest clean grading by the
current judge. The only score used is the deterministic Final calculation
accuracy check, scored_results.accuracy_engine.checks
["Accuracy/Final calculation accuracy"], as decided by the harness answer
checker (judge/utils/answer_check.py): a task passes when every Questions-sheet
answer matches the golden. A grading whose check fell back to the LLM is left
out. A cohort's accuracy is the share of its tasks that pass; for the cohorts in
plot_leaderboard.BACKFILL_ZERO a live task with no grading counts as a fail. The attempt's
task_attempts.time_taken_min gives the completion time. Tasks are bucketed by
tasks.human_difficulty_measure; each task's expert estimate,
case_classification.time_assumption_h, comes from the task-analysis JSON written
by plot_difficulty_and_types.py (newest unless --results is given).

Only the cohorts in ROSTER are plotted (display names from style_guide.yaml
`agents`, which gives each its vendor and the database agent_model_names pooled
into it, the newest grading kept when two of them cover a task).

Draws accuracy_by_difficulty.pdf (a .png alongside under --png) under
operation/results/v2/plots/accuracy_by_difficulty/, at the printed width
(style_guide.yaml `print`) so nothing is scaled in the paper:

  Top           accuracy leaderboard over the selected difficulties
                (--difficulty, default Medium and harder): one column per
                cohort, best at the left, the pass share with a standard-error
                whisker, and "n/N" in slate when the cohort has not covered
                every selected task. Columns carry the leaderboard's encoding:
                vendor hue, harness tint and hatch, squircle cap.
  Bottom left   mean accuracy of the selected cohorts (--models or --top, default
                BOTTOM_DEFAULT) at each difficulty bucket, easiest
                to hardest (E, M, M-H, H): a PCHIP-smoothed line through the
                bucket means with a translucent standard-error ribbon, and a
                harness marker at each bucket (GUI circle, Excel square, Code
                diamond, In-house triangle).
  Bottom middle the same cohorts' mean solve time per bucket, hours on a log
                axis, drawn the same way.
  Bottom right  agent time against the expert estimate per task, hours on both
                log axes: a faint dot per task, a least-squares line per cohort
                with its harness marker and its slope at the right end, and the
                Expert Pace (agent hours = expert hours) as a dashed slate line.

The cohorts on the bottom row are named in a legend beneath it, each with the
Spearman rho between its time and the expert's. --models takes
display names or agent_model_name keys, case-insensitively. Run with the shared
plotting environment, ~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --png --top 4 --no-smooth
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --models "Fable 5.1-Code" "Astra-Code"
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --difficulty Medium-Hard Hard
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --time-scale linear
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --results operation/results/v2/task_analysis/<stamp>.json
    python operation/v2/paper_scripts/plot_accuracy_by_difficulty.py --database-url postgresql://...
"""

import argparse
import json
import math
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
from scipy.interpolate import PchipInterpolator
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    DIFFICULTY_ORDER,
    INK,
    REPO_ROOT,
    STYLE,
    SURFACE,
    UNKNOWN_COLOR,
    axis_title,
    database_url,
    order_key,
    quiet_axes,
)
from plot_leaderboard import (  # noqa: E402
    BACKFILL_ZERO,
    COHORTS,
    COMPANY_COLORS,
    HARNESS,
    HARNESS_HATCH,
    HARNESS_ORDER,
    HATCH_ALPHA,
    LEGEND_HANDLE_HEIGHT,
    LEGEND_HANDLE_LENGTH,
    LEGEND_HARNESS_FACE,
    HATCH_DENSITY,
    LEGEND_WEIGHT,
    MIN_JUDGE_VERSION,
    SLATE,
    YLIM,
    YTICKS,
    _HatchedHandler,
    _tinted,
    db_names,
    draw_columns,
    fold_cohorts,
    name_tilt,
    save_figure,
    text_width_in,
)
from plot_solve_time import newest_results  # noqa: E402

PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "accuracy_by_difficulty"
PLOT_NAME = "accuracy_by_difficulty"
DIFFICULTY_DEFAULT = ("Medium", "Medium-Hard", "Hard")  # the leaderboard's task set
# Bottom-row cohorts when neither --models nor --top is given: four strong agents whose time scales with the
# expert's (high rho and slope), then two weak ones whose time barely moves (low rho and slope)
BOTTOM_DEFAULT = ("Fable 5.1", "Fable 5.1-Code", "Grok 4.6-Code", "Astra-Work", "Gemini 3.8-Code", "Gemini 3.8")
FINAL_ACCURACY = "Accuracy/Final calculation accuracy"  # key in scored_results.accuracy_engine.checks

# The cohorts in this figure, by display name (style_guide.yaml `agents`, which holds their vendors and database names).
ROSTER = (
    # GUI
    "Fable 5.1-Cowork",
    "Astra-Work",
    "GPT-6-Pro",
    # Excel
    "Fable 5.1-Excel",
    "Opus 5-Excel",
    "Sol-Excel",
    # Code
    "Fable 5.1-Code",
    "Astra-Code",
    "Gemini 3.8-Code",
    "Grok 4.6-Code",
    "Kimi K3-Code",
    # In-house
    "Fable 5.1",
    "Astra",
    "Gemini 3.8",
    "Grok 4.6",
)

# ============================================================================
# FIGURE STYLE
# ============================================================================
# The canvas is the printed size (style_guide.yaml print.textwidth_in, included with
# width=\textwidth), so every font size below is the size that prints. The tilted
# cohort names hang left of and below the leaderboard; both margins are measured from
# the rendered names.
PRINT = STYLE["print"]
FIG_W_IN = PRINT["textwidth_in"]

BOARD_H_IN = 1.36  # the leaderboard; its y-axis title 'Accuracy, Medium+ (%)' runs 1.33 in, so no shorter
LINE_H_IN = 1.05  # each of the three panels on the bottom row
TITLE_IN = 0.2  # above each bottom panel, for its title
ROW_GAP_IN = 0.16  # between the deepest tilted name and the bottom panels' titles; the row rule sits midway
LINE_GAP_IN = 0.5  # between the bottom panels; each has its own y ticks
TOP_PAD_IN = 0.06  # above the leaderboard, for the half-height of its top tick number
YLABEL_IN = 0.42  # y-axis title plus tick numbers
RIGHT_IN = 0.04
NAME_PAD_IN = 0.06  # below the deepest tilted name
BUCKET_IN = 0.17  # bucket abbreviations under the bottom panels
PACE_XTITLE_IN = 0.19  # what the pace panel's x-axis title hangs below the bucket names
COHORT_LEGEND_IN = 0.32  # cohort legend beneath the bottom row (two rows)
BOARD_X_PAD = 0.6  # columns of empty space before the first and after the last column
LEGEND_ROW_IN = 0.16  # one row of the vendor/harness key, inside the leaderboard's top right
LEGEND_TITLE_PAD_PT = 5  # "Vendor" / "Harness" sit this far left of their row's entries
BUCKET_ABBREV = {"Easy": "E", "Medium": "M", "Medium-Hard": "M-H", "Hard": "H"}
ROW_RULE_COLOR = STYLE["ink"]["slate_light"]  # hairline between the leaderboard and the bottom row
ROW_RULE_LW = 0.5
ROW_RULE_INSET_IN = 0.15  # the rule stops this far short of each side of the figure

TITLE_FS = PRINT["pt"]["header"]
TITLE_WEIGHT = "bold"  # bottom panel titles in Avenir Heavy, apart from the Book cohort names above
NAME_FS = PRINT["pt"]["min"]  # cohort names under the leaderboard, at the floor to save height
TICK_FS = PRINT["pt"]["label"]  # bucket names under the bottom panels
BUCKET_WEIGHT = 500  # bucket abbreviations in Avenir Medium, a step above the Book tick numbers
LEGEND_FS = PRINT["pt"]["label"]

LINE_LW = 1.2
LINE_ALPHA = 0.9
BAND_ALPHA = 0.14
MARKER_SIZE = 4.0
MARKER_EDGE_LW = 0.6
HARNESS_MARKER = {"GUI": "o", "Excel": "s", "Code": "D", "In-house": "^"}
LINE_X_PAD = 0.25  # x units beyond the first and last bucket
ACCURACY_YLIM = (0, 105)  # headroom so a marker at 100 is not clipped by the axes edge
SMOOTH_SAMPLES = 200  # interpolated points per line under --smooth
TIME_TICK_CANDIDATES = (0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50)  # hours
TIME_LOG_HEADROOM = 1.25  # y-limit factor above the highest band edge on a log axis
TIME_LINEAR_HEADROOM = 1.1
COHORT_LEGEND_NCOL = 3
PACE_DOT_SIZE = 3  # scatter marker area in points^2
PACE_DOT_ALPHA = 0.3
PACE_INK = STYLE["ink"]["slate_dark"]  # the Expert Pace line and its label
PACE_LW = 0.8
PACE_DASH = (0, (3, 1.6))
PACE_NOTE_FS = PRINT["pt"]["min"]
PACE_LOG_HEADROOM = 1.2  # limits as a factor beyond the shortest and longest task
PACE_X_TICKS = (0.3, 1, 3, 10, 30)  # expert hours, half a decade apart to fit the narrow panel
PACE_Y_TICKS = (0.03, 0.1, 0.3, 1, 3, 10, 30)  # agent hours
MIN_PACE_POINTS = 3  # a cohort needs this many timed tasks for a correlation and a fit
COHORT_HANDLE_LENGTH = 2.0  # line with the marker at its middle
COHORT_LEGEND_MARKER = 5.5  # legend markers, a step larger than on the lines so the harness shape reads
COHORT_COLUMN_GAP_EM = 0.8  # between one column's rho text and the next column's line
RHO_PAD_IN = 0.05  # between a column's widest name and its rho texts
SLOPE_DX_PT = 5  # slope labels start this far right of each fit's end marker
SLOPE_GAP_PT = 8  # minimum vertical spacing between slope labels
SLOPE_IN = 0.26  # right of the pace panel, for the slope labels

SQL = """
    select distinct on (ta.agent_model_name, ta.task_id)
           ta.agent_model_name, ta.agent_model_type, ta.task_id, ta.time_taken_min,
           t.human_difficulty_measure as difficulty,
           g.scored_results->'accuracy_engine'->'checks'->%(check)s->>'engine' as engine,
           g.scored_results->'accuracy_engine'->'checks'->%(check)s->>'decision' as decision,
           g.created_at as graded_at
    from gradings g
    join task_attempts ta on ta.id = g.attempt_id
    join tasks t on t.id = ta.task_id
    where g.judge_version >= %(judge)s
      and g.failed is not true and g.deprecated is not true
      and ta.deprecated is not true and t.deprecated is not true
      and g.scored_results->>'total_score' is not null
      and ta.agent_model_name = any(%(roster)s)
    order by ta.agent_model_name, ta.task_id, g.created_at desc
"""


@dataclass
class Cohort:
    key: str
    name: str
    company: str
    harness: str
    scores: dict[str, list[float]] = field(default_factory=dict)  # difficulty -> 100 pass / 0 fail
    minutes: dict[str, list[float]] = field(default_factory=dict)  # difficulty -> completion times
    task_minutes: dict[int, float] = field(default_factory=dict)  # task id -> completion time
    pace: list[tuple[float, float]] = field(default_factory=list)  # (expert hours, agent hours) per task

    def over(self, difficulties) -> list[float]:
        return [s for d in difficulties for s in self.scores.get(d, [])]

    @property
    def color(self) -> str:
        """Vendor hue, mixed toward the surface by the harness tint."""
        return _tinted(COMPANY_COLORS.get(self.company, UNKNOWN_COLOR), self.harness)

    def rho(self) -> float:
        """Spearman rho of agent time on the expert estimate; nan below MIN_PACE_POINTS."""
        if len(self.pace) < MIN_PACE_POINTS:
            return float("nan")
        return float(spearmanr(*zip(*self.pace))[0])

    def fit(self) -> tuple[float, float]:
        """(slope, intercept) of log(agent hours) on log(expert hours)."""
        e, h = np.log(np.array(self.pace)).T
        slope, intercept = np.polyfit(e, h, 1)
        return float(slope), float(intercept)


def mean_se(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return float("nan"), 0.0
    se = float(np.std(values, ddof=1) / np.sqrt(n)) if n > 1 else 0.0
    return float(np.mean(values)), se


# --- data ----------------------------------------------------------------------


def fetch(conn) -> tuple[dict[str, Cohort], dict[str, int]]:
    """Every ROSTER cohort with its per-difficulty accuracies and times, and live tasks per difficulty."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select id, human_difficulty_measure as d from tasks where deprecated is not true")
    live = {r["id"]: r["d"] for r in cur.fetchall()}
    tasks_per_difficulty: dict[str, int] = {}
    for d in live.values():
        tasks_per_difficulty[d] = tasks_per_difficulty.get(d, 0) + 1
    cur.execute(SQL, {"judge": MIN_JUDGE_VERSION, "check": FINAL_ACCURACY, "roster": db_names(ROSTER)})
    rows = fold_cohorts(cur.fetchall())

    by_name: dict[str, Cohort] = {}
    not_harness = no_time = 0
    for r in rows:
        if r["engine"] != "harness" or r["decision"] not in ("pass", "fail"):
            not_harness += 1
            continue
        name = r["cohort"]
        if name not in by_name:
            company, dbs = COHORTS[name]
            by_name[name] = Cohort(dbs[0], name, company, HARNESS.get(r["agent_model_type"], "?"))
        c = by_name[name]
        c.scores.setdefault(r["difficulty"], []).append(100.0 if r["decision"] == "pass" else 0.0)
        if r["time_taken_min"] is not None and r["time_taken_min"] > 0:
            c.minutes.setdefault(r["difficulty"], []).append(float(r["time_taken_min"]))
            c.task_minutes[r["task_id"]] = float(r["time_taken_min"])
        else:
            no_time += 1

    # BACKFILL_ZERO cohorts: a live task with no grading at all fails (no time); one graded but
    # decided by the LLM stays out, as for every cohort
    for name in BACKFILL_ZERO & by_name.keys():
        missing = live.keys() - {r["task_id"] for r in rows if r["cohort"] == name}
        if missing:
            print(f"{name}: {len(missing)} ungraded tasks backfilled as failed", file=sys.stderr)
        for t in missing:
            by_name[name].scores.setdefault(live[t], []).append(0.0)

    if not_harness:
        print(f"skipping {not_harness} gradings whose Final calculation accuracy was not decided by the harness",
              file=sys.stderr)
    if no_time:
        print(f"{no_time} graded attempts have no positive time_taken_min; left out of the time panel",
              file=sys.stderr)
    for name in ROSTER:
        if name not in by_name:
            print(f"skipping {name}: no graded tasks", file=sys.stderr)
    for c in by_name.values():
        if c.company not in COMPANY_COLORS:
            print(f"{c.name}: vendor {c.company!r} has no colour in style_guide.yaml", file=sys.stderr)
    return by_name, tasks_per_difficulty


def leaderboard(cohorts: dict[str, Cohort], difficulties: tuple[str, ...]) -> list[Cohort]:
    kept = []
    for c in cohorts.values():
        if not c.over(difficulties):
            print(f"skipping {c.name}: no graded tasks on {', '.join(difficulties)}", file=sys.stderr)
            continue
        kept.append(c)
    return sorted(kept, key=lambda c: -mean_se(c.over(difficulties))[0])


def select_models(cohorts: dict[str, Cohort], ranked: list[Cohort], names: list[str] | None,
                  top: int | None) -> list[Cohort]:
    """--models by display name or key (case-insensitive), else --top of the leaderboard, else BOTTOM_DEFAULT."""
    if not names and top:
        return ranked[:top]
    names = names or list(BOTTOM_DEFAULT)
    lookup = {c.name.lower(): c for c in cohorts.values()}
    lookup.update({c.key.lower(): c for c in cohorts.values()})
    chosen = []
    for name in names:
        c = lookup.get(name.lower())
        if c is None:
            sys.exit(f"--models: {name!r} is not a graded cohort in ROSTER")
        if c not in chosen:
            chosen.append(c)
    return chosen


# --- figure ----------------------------------------------------------------------


def plot_leaderboard_panel(ax, ranked: list[Cohort], difficulties: tuple[str, ...], n_tasks: int,
                           theta: float) -> None:
    cols = [(float(i), c) for i, c in enumerate(ranked)]
    quiet_axes(ax)
    ax.set_xlim(-BOARD_X_PAD, cols[-1][0] + BOARD_X_PAD)
    ax.set_ylim(*YLIM)
    ax.set_yticks(list(YTICKS))
    axis_title(ax, f"Accuracy, {_difficulty_phrase(difficulties)} (%)")
    ax.set_xticks([x for x, _ in cols], [c.name for _, c in cols], fontsize=NAME_FS, color=INK,
                  rotation=math.degrees(theta), ha="right", rotation_mode="anchor")
    values = []
    for _, c in cols:
        scores = c.over(difficulties)
        m, se = mean_se(scores)
        values.append((m, se, f"{m:.0f}%", f"{len(scores)}/{n_tasks}" if len(scores) < n_tasks else None))
    draw_columns(ax, cols, values, mcolors.to_rgba(SURFACE, HATCH_ALPHA))


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
    ax.set_xlim(-LINE_X_PAD, len(buckets) - 1 + LINE_X_PAD)
    ax.set_xticks(range(len(buckets)), [BUCKET_ABBREV.get(d, d) for d in buckets], fontsize=TICK_FS, color=INK,
                  fontweight=BUCKET_WEIGHT)
    return lows, highs


def plot_accuracy_panel(ax, selected: list[Cohort], buckets: list[str], smooth: bool) -> None:
    quiet_axes(ax)
    draw_lines(ax, selected, buckets, lambda c, d: c.scores.get(d, []), smooth)
    ax.set_ylim(*ACCURACY_YLIM)
    ax.set_yticks(list(YTICKS))
    axis_title(ax, "Accuracy (%)")
    ax.set_title("Accuracy by Difficulty", fontsize=TITLE_FS, color=INK, fontweight=TITLE_WEIGHT, pad=4)


def plot_time_panel(ax, selected: list[Cohort], buckets: list[str], smooth: bool, log: bool) -> None:
    """Mean completion time per bucket, in hours to match the pace panel."""
    quiet_axes(ax)
    if log:
        ax.set_yscale("log")
    hours = lambda c, d: [m / 60 for m in c.minutes.get(d, [])]  # noqa: E731
    positive = [h for c in selected for d in buckets for h in hours(c, d)]
    floor = min(positive) / 2 if (log and positive) else None
    lows, highs = draw_lines(ax, selected, buckets, hours, smooth, floor=floor)
    if highs:
        if log:
            lo, hi = min(lows) / TIME_LOG_HEADROOM, max(highs) * TIME_LOG_HEADROOM
        else:
            lo, hi = 0.0, max(highs) * TIME_LINEAR_HEADROOM
        ax.set_ylim(lo, hi)
        if log:
            ticks = [t for t in TIME_TICK_CANDIDATES if lo <= t <= hi]
            ax.set_yticks(ticks, [f"{t:g}" for t in ticks])
            ax.yaxis.set_minor_locator(plt.NullLocator())
    axis_title(ax, "Agent solve time (h)")
    ax.set_title("Solve Time by Difficulty", fontsize=TITLE_FS, color=INK, fontweight=TITLE_WEIGHT, pad=4)


def _log_ticks(ax, axis: str, candidates, lo: float, hi: float) -> None:
    ticks = [t for t in candidates if lo <= t <= hi]
    (ax.set_xticks if axis == "x" else ax.set_yticks)(ticks, [f"{t:g}" for t in ticks])
    (ax.xaxis if axis == "x" else ax.yaxis).set_minor_locator(plt.NullLocator())


def plot_pace_panel(ax, selected: list[Cohort]) -> None:
    """Agent hours against expert hours, log-log: dots, a fitted line per cohort, the Expert Pace."""
    fitted = [c for c in selected if len(c.pace) >= MIN_PACE_POINTS]
    xs = [e for c in fitted for e, _ in c.pace]
    ys = [h for c in fitted for _, h in c.pace]
    x_lo, x_hi = min(xs) / PACE_LOG_HEADROOM, max(xs) * PACE_LOG_HEADROOM
    y_lo, y_hi = min(ys) / PACE_LOG_HEADROOM, max(ys) * PACE_LOG_HEADROOM
    for c in fitted:
        e, h = np.array(c.pace).T
        ax.scatter(e, h, s=PACE_DOT_SIZE, color=c.color, alpha=PACE_DOT_ALPHA, linewidth=0, zorder=1)
    for c in fitted:
        e = np.array(c.pace)[:, 0]
        slope, intercept = c.fit()
        x_line = np.geomspace(e.min(), e.max(), 50)
        y_line = np.exp(intercept) * x_line ** slope
        ax.plot(x_line, y_line, color=c.color, lw=LINE_LW, alpha=LINE_ALPHA, solid_capstyle="round", zorder=2)
        ax.plot(x_line[-1:], y_line[-1:], linestyle="none", marker=HARNESS_MARKER.get(c.harness, "o"),
                ms=MARKER_SIZE, mfc=c.color, mec=SURFACE, mew=MARKER_EDGE_LW, zorder=3)
    x_pace = np.geomspace(x_lo, x_hi, 50)
    ax.plot(x_pace, x_pace, color=PACE_INK, lw=PACE_LW, ls=PACE_DASH, zorder=1.5)
    x_note = min(x_hi, y_hi) / 3  # well below where the line leaves the panel, clear of the title
    ax.text(x_note / 1.08, x_note, "Expert Pace", ha="right", va="bottom",  # above-left, clear of the fits
            fontsize=PACE_NOTE_FS, color=PACE_INK, style="italic")
    quiet_axes(ax, value_axis="both")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_lo, y_hi)
    _log_ticks(ax, "x", PACE_X_TICKS, x_lo, x_hi)
    _log_ticks(ax, "y", PACE_Y_TICKS, y_lo, y_hi)
    for label in ax.get_xticklabels():  # x ticks in ink, as on the bucket axes
        label.set_color(INK)
    axis_title(ax, "Expected solve time (h)", axis="x")
    ax.xaxis.label.set_color(INK)
    axis_title(ax, "Agent solve time (h)")
    ax.set_title("Agent vs. Expert Time", fontsize=TITLE_FS, color=INK, fontweight=TITLE_WEIGHT, pad=4)
    label_slopes(ax, fitted)


def label_slopes(ax, fitted: list[Cohort]) -> None:
    """Each fit's slope just right of its end marker, nudged apart so none overlap, under a 'slope' header."""
    pt_per_px = 72 / ax.figure.dpi
    ends = []
    for c in fitted:
        slope, intercept = c.fit()
        x_end = np.array(c.pace)[:, 0].max()
        y_end = np.exp(intercept) * x_end ** slope
        ends.append((ax.transData.transform((x_end, y_end))[1] * pt_per_px, x_end, y_end, slope, c))
    ends.sort(key=lambda e: e[0])
    placed = []  # label heights in points, bottom-up, each at least SLOPE_GAP_PT above the last
    for y_pt, *_ in ends:
        placed.append(max(y_pt, placed[-1] + SLOPE_GAP_PT) if placed else y_pt)
    for (y_pt, x_end, y_end, slope, c), y_label in zip(ends, placed):
        ax.annotate(f"{slope:.2f}", xy=(x_end, y_end), xytext=(SLOPE_DX_PT, y_label - y_pt),
                    textcoords="offset points", ha="left", va="center", fontsize=PACE_NOTE_FS,
                    color=c.color, fontweight=LEGEND_WEIGHT, annotation_clip=False)
    if ends:
        _, x_end, y_end, *_ = ends[-1]
        ax.annotate("slope", xy=(x_end, y_end), xytext=(SLOPE_DX_PT, placed[-1] - ends[-1][0] + SLOPE_GAP_PT),
                    textcoords="offset points", ha="left", va="center", fontsize=PACE_NOTE_FS, color=SLATE,
                    annotation_clip=False)


def add_key(fig, ax, cohorts: list[Cohort]) -> None:
    """Vendor and harness rows in the leaderboard's top right, each titled just left of its entries."""
    vendors = []
    for c in cohorts:
        if c.company not in vendors:
            vendors.append(c.company)
    hatch_color = mcolors.to_rgba(SURFACE, HATCH_ALPHA)
    v_handles = [mpatches.Patch(facecolor=COMPANY_COLORS.get(v, UNKNOWN_COLOR), edgecolor="none")
                 for v in vendors]
    harnesses = [h for h in HARNESS_ORDER if any(c.harness == h for c in cohorts)]
    h_handles = [mpatches.Patch(facecolor=_tinted(LEGEND_HARNESS_FACE, h), edgecolor=hatch_color,
                                hatch=HARNESS_HATCH[h] * HATCH_DENSITY)
                 for h in harnesses]
    common = dict(frameon=False, prop={"size": LEGEND_FS, "weight": LEGEND_WEIGHT}, labelcolor=INK,
                  handleheight=LEGEND_HANDLE_HEIGHT, columnspacing=1.0,
                  handletextpad=0.4, borderpad=0, borderaxespad=0,
                  handler_map={mpatches.Patch: _HatchedHandler()})
    row_frac = LEGEND_ROW_IN / BOARD_H_IN
    rows = (("Vendor", v_handles, vendors, LEGEND_HANDLE_LENGTH),
            ("Harness", h_handles, harnesses, LEGEND_HANDLE_LENGTH))
    for row, (title, handles, labels, handlelength) in enumerate(rows):
        y = 1 - (row + 0.5) * row_frac
        leg = ax.legend(handles=handles, labels=labels, loc="center right", ncol=len(handles),
                        bbox_to_anchor=(1, y), handlelength=handlelength, **common)
        ax.add_artist(leg)  # a second axes.legend() call would replace the first
        fig.canvas.draw()  # the row's extent is only known once drawn
        # the title lines up with the entry labels' centre, not the row's, which the swatches skew
        label = leg.get_texts()[0].get_window_extent()
        left, y_label = ax.transAxes.inverted().transform((leg.get_window_extent().x0, (label.y0 + label.y1) / 2))
        ax.annotate(title, xy=(left, y_label), xycoords="axes fraction", xytext=(-LEGEND_TITLE_PAD_PT, 0),
                    textcoords="offset points", ha="right", va="center", fontsize=LEGEND_FS, color=SLATE,
                    annotation_clip=False)


def make_figure(ranked: list[Cohort], selected: list[Cohort], difficulties: tuple[str, ...],
                buckets: list[str], n_tasks: int, smooth: bool, log_time: bool) -> plt.Figure:
    fig = plt.figure(figsize=(FIG_W_IN, 1.0), facecolor=SURFACE)  # height follows the margins

    # leaderboard margins from the rendered names: each hangs w*cos(theta) left and w*sin(theta)
    # below its column, tilted only as far as they must to clear each other (name_tilt); the
    # left one competes with the y-axis title, and the axes take the rest
    widths = [(i, text_width_in(fig, c.name, NAME_FS)) for i, c in enumerate(ranked)]
    n_pitch = len(ranked) - 1 + 2 * BOARD_X_PAD
    left_in = YLABEL_IN
    for _ in range(5):  # tilt, spill and column pitch depend on each other
        pitch_in = (FIG_W_IN - left_in - RIGHT_IN) / n_pitch
        theta = name_tilt(pitch_in, NAME_FS)
        spill = max(w * math.cos(theta) - (x + BOARD_X_PAD) * pitch_in for x, w in widths)
        left_in = max(YLABEL_IN, spill + NAME_PAD_IN)
    names_in = max(w * math.sin(theta) for _, w in widths) + NAME_PAD_IN

    # bottom-up: cohort legend, the pace panel's x title, bucket names, the three bottom panels,
    # their titles, the tilted names, leaderboard
    line_bottom_in = COHORT_LEGEND_IN + PACE_XTITLE_IN + BUCKET_IN
    board_bottom_in = line_bottom_in + LINE_H_IN + TITLE_IN + ROW_GAP_IN + names_in
    fig_h = board_bottom_in + BOARD_H_IN + TOP_PAD_IN
    fig.set_size_inches(FIG_W_IN, fig_h)

    full_w_in = FIG_W_IN - left_in - RIGHT_IN
    line_w_in = (full_w_in - 2 * LINE_GAP_IN - SLOPE_IN) / 3
    ax_board = fig.add_axes([left_in / FIG_W_IN, board_bottom_in / fig_h, full_w_in / FIG_W_IN, BOARD_H_IN / fig_h])
    ax_acc, ax_time, ax_pace = (
        fig.add_axes([(left_in + i * (line_w_in + LINE_GAP_IN)) / FIG_W_IN, line_bottom_in / fig_h,
                      line_w_in / FIG_W_IN, LINE_H_IN / fig_h])
        for i in range(3)
    )
    plot_leaderboard_panel(ax_board, ranked, difficulties, n_tasks, theta)
    plot_accuracy_panel(ax_acc, selected, buckets, smooth)
    plot_time_panel(ax_time, selected, buckets, smooth, log_time)
    plot_pace_panel(ax_pace, selected)
    add_key(fig, ax_board, ranked + [c for c in selected if c not in ranked])

    # hairline midway between the deepest tilted name and the tallest bottom title, as rendered
    fig.canvas.draw()
    names_bottom = min(t.get_window_extent().y0 for t in ax_board.get_xticklabels())
    titles_top = max(ax.title.get_window_extent().y1 for ax in (ax_acc, ax_time, ax_pace))
    rule_y = fig.transFigure.inverted().transform((0, (names_bottom + titles_top) / 2))[1]
    # inset from the drawn content's edges, which the saved (tight-cropped) figure ends at, not the canvas
    content = fig.get_tightbbox(fig.canvas.get_renderer())  # inches
    x0, x1 = (content.x0 + ROW_RULE_INSET_IN) / FIG_W_IN, (content.x1 - ROW_RULE_INSET_IN) / FIG_W_IN
    fig.add_artist(mlines.Line2D([x0, x1], [rule_y, rule_y], transform=fig.transFigure,
                                 color=ROW_RULE_COLOR, lw=ROW_RULE_LW))

    add_cohort_legend(fig, selected, 0.5 * (left_in + FIG_W_IN - RIGHT_IN) / FIG_W_IN)
    return fig


def add_cohort_legend(fig, selected: list[Cohort], x_center: float) -> None:
    """The bottom row's cohorts beneath it, each entry the series as drawn (line, harness marker), then
    its agent-vs-expert rho as '(ρ = 0.83)', left-aligned down each legend column.

    The legend holds only the names; each column is widened by the rho text's width, and the rho texts
    are placed after drawing, at the right edge of the column's widest name.
    """
    handles = [mlines.Line2D([], [], color=c.color, lw=LINE_LW, alpha=LINE_ALPHA, solid_capstyle="round",
                             marker=HARNESS_MARKER.get(c.harness, "o"), ms=COHORT_LEGEND_MARKER, mfc=c.color,
                             mec=SURFACE, mew=MARKER_EDGE_LW, label=c.name) for c in selected]
    rhos = [c.rho() for c in selected]
    rho_texts = [None if math.isnan(r) else f"(ρ = {r:.2f})" for r in rhos]
    prop = {"size": LEGEND_FS, "weight": LEGEND_WEIGHT}
    probe = fig.text(0, 0, "(ρ = 0.00)", fontsize=LEGEND_FS, fontweight=LEGEND_WEIGHT)
    rho_w_in = probe.get_window_extent(fig.canvas.get_renderer()).width / fig.dpi + RHO_PAD_IN
    probe.remove()
    rho_w_em = rho_w_in * 72 / LEGEND_FS  # legend spacings are in font sizes
    # the last column's rho hangs past the legend box, so shift the box left by half of it to stay centred
    leg = fig.legend(handles=handles, loc="lower center",
                     bbox_to_anchor=(x_center - 0.5 * rho_w_in / FIG_W_IN, 0),
                     ncol=COHORT_LEGEND_NCOL, prop=prop, labelcolor=INK, frameon=False,
                     handlelength=COHORT_HANDLE_LENGTH, handletextpad=0.5,
                     columnspacing=COHORT_COLUMN_GAP_EM + rho_w_em, labelspacing=0.3, borderpad=0, borderaxespad=0)
    fig.canvas.draw()  # the names' extents are only known once drawn
    texts = leg.get_texts()
    n_rows = math.ceil(len(texts) / COHORT_LEGEND_NCOL)
    inv = fig.transFigure.inverted()
    for col in range(COHORT_LEGEND_NCOL):
        idx = list(range(col * n_rows, min((col + 1) * n_rows, len(texts))))  # legends fill column-first
        if not idx:
            continue
        x_px = max(texts[i].get_window_extent().x1 for i in idx) + RHO_PAD_IN * fig.dpi
        for i in idx:
            if rho_texts[i] is None:
                continue
            ext = texts[i].get_window_extent()
            x, y = inv.transform((x_px, (ext.y0 + ext.y1) / 2))
            fig.text(x, y, rho_texts[i], ha="left", va="center", fontsize=LEGEND_FS, fontweight=LEGEND_WEIGHT,
                     color=INK)


def _difficulty_phrase(difficulties: tuple[str, ...]) -> str:
    """'Medium and harder' when the selection is a tail of the ladder, else the list."""
    ordered = sorted(difficulties, key=order_key(DIFFICULTY_ORDER))
    tail = DIFFICULTY_ORDER[DIFFICULTY_ORDER.index(ordered[0]):] if ordered[0] in DIFFICULTY_ORDER else ()
    if tuple(ordered) == tail and len(ordered) > 1:
        return f"{ordered[0]}+"
    return ", ".join(ordered)


# --- report ----------------------------------------------------------------------


def report(ranked: list[Cohort], selected: list[Cohort], difficulties: tuple[str, ...],
           buckets: list[str], tasks_per_difficulty: dict[str, int]) -> None:
    n_tasks = sum(tasks_per_difficulty.get(d, 0) for d in difficulties)
    print(f"{len(ranked)} cohorts on {', '.join(difficulties)} ({n_tasks} live tasks), "
          f"judge >= v{MIN_JUDGE_VERSION}, deterministic Final calculation accuracy only")
    print(f"{'#':>3}  {'cohort':<34}{'harness':<10}{'vendor':<11}{'n':>4}{'pass%':>7}{'se':>6}")
    for i, c in enumerate(ranked, 1):
        scores = c.over(difficulties)
        m, se = mean_se(scores)
        print(f"{i:>3}  {c.name:<34}{c.harness:<10}{c.company:<11}{len(scores):>4}{m:>7.1f}{se:>6.1f}")

    live = ", ".join(f"{d} {tasks_per_difficulty.get(d, 0)}" for d in buckets)
    for title, attr in (("accuracy, % of tasks passing", "scores"), ("completion time, minutes", "minutes")):
        print(f"\n{title} by difficulty, mean ± se (n); live tasks: {live}")
        print(f"{'cohort':<34}" + "".join(f"{d:>20}" for d in buckets))
        for c in selected:
            cells = []
            for d in buckets:
                vals = getattr(c, attr).get(d, [])
                m, se = mean_se(vals)
                cells.append(f"{m:.1f} ± {se:.1f} ({len(vals)})" if vals else "-")
            print(f"{c.name:<34}" + "".join(f"{cell:>20}" for cell in cells))

    print("\nagent time vs expert estimate (* = on the bottom row); slope of log(agent h) on log(expert h),"
          " 1 = same scaling as the expert; speed-up = median of expert h / agent h")
    print(f"   {'cohort':<34}{'harness':<10}{'n':>4}{'rho':>7}{'slope':>7}{'speed-up':>10}")
    for c in sorted((c for c in ranked if len(c.pace) >= MIN_PACE_POINTS), key=lambda c: -c.rho()):
        e, h = np.array(c.pace).T
        mark = "*" if c in selected else " "
        print(f" {mark} {c.name:<34}{c.harness:<10}{len(e):>4}{c.rho():>7.2f}{c.fit()[0]:>7.2f}"
              f"{np.median(e / h):>9.1f}x")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", help="full connection string; bypasses config/config.yaml")
    parser.add_argument("--difficulty", nargs="+", default=list(DIFFICULTY_DEFAULT), metavar="BUCKET",
                        help=f"difficulty buckets the leaderboard ranks on (default: {' '.join(DIFFICULTY_DEFAULT)})")
    parser.add_argument("--models", nargs="+", metavar="NAME",
                        help="cohorts on the bottom row, by display name or agent_model_name (default: BOTTOM_DEFAULT)")
    parser.add_argument("--top", type=int, default=None,
                        help="follow this many leaderboard cohorts on the bottom row instead of BOTTOM_DEFAULT (ignored with --models)")
    parser.add_argument("--no-smooth", dest="smooth", action="store_false",
                        help="straight segments between buckets instead of the PCHIP curve")
    parser.add_argument("--time-scale", choices=["log", "linear"], default="log",
                        help="y scale of the completion-time panel (default log)")
    parser.add_argument("--results", type=Path, default=None,
                        help="task-analysis JSON with the expert estimates (default: newest from plot_difficulty_and_types.py)")
    parser.add_argument("--png", action="store_true", help="also save a .png preview next to the .pdf")
    parser.add_argument("--plot-dir", type=Path, default=PLOT_DIR,
                        help=f"directory the figure is saved to as {PLOT_NAME}.pdf (default: {PLOT_DIR})")
    args = parser.parse_args()

    missing = [n for n in ROSTER if n not in COHORTS]
    if missing:
        sys.exit(f"ROSTER names cohorts with no entry in style_guide.yaml `agents`: {missing}")
    difficulties = tuple(args.difficulty)
    unknown = [d for d in difficulties if d not in DIFFICULTY_ORDER]
    if unknown:
        sys.exit(f"--difficulty: unknown bucket(s) {unknown}; choose from {DIFFICULTY_ORDER}")

    conn = psycopg2.connect(database_url(args))
    try:
        cohorts, tasks_per_difficulty = fetch(conn)
    finally:
        conn.close()
    results = args.results or newest_results()
    expert_h = {t["id"]: t["time_h"] for t in json.loads(results.read_text())["tasks"]}
    print(f"expert estimates from {results}")
    for c in cohorts.values():
        c.pace = [(expert_h[t], m / 60) for t, m in c.task_minutes.items() if t in expert_h]
    ranked = leaderboard(cohorts, difficulties)
    if not ranked:
        sys.exit("no ROSTER cohort is graded on the selected difficulties")
    selected = select_models(cohorts, ranked, args.models, args.top)
    buckets = sorted((d for d in tasks_per_difficulty if d in DIFFICULTY_ORDER), key=order_key(DIFFICULTY_ORDER))
    report(ranked, selected, difficulties, buckets, tasks_per_difficulty)

    n_tasks = sum(tasks_per_difficulty.get(d, 0) for d in difficulties)
    fig = make_figure(ranked, selected, difficulties, buckets, n_tasks, args.smooth, args.time_scale == "log")
    for path in save_figure(fig, args.plot_dir, PLOT_NAME, png=args.png):
        print(f"wrote {path}")
    plt.close(fig)


if __name__ == "__main__":
    main()
