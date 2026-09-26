#!/usr/bin/env python
"""Pass-rate leaderboard of every graded v2 agent cohort, one column each, best at the left.

Reads the live v2 database (config database.v2_url): for each (agent, task) the
newest clean grading by the current judge (judge_version >= MIN_JUDGE_VERSION,
not failed, not deprecated, attempt and task not deprecated), its
scored_results.total_score (the 0-100 composite over the twelve rubric_9
categories) and its per-check verdicts in scored_results.check_scores. A cohort
graded on fewer than --min-tasks tasks is left out. Columns are ordered by pass
rate (ties by composite score); under --with-score by composite score instead.

A task passes when Final calculation accuracy (decided deterministically, so
never a false flag) is not flagged and, for each rubrics.csv Importance tier t
(0-3), at most k_t of that tier's other checks are; the "Pass Rate Derivation" box in pass_rule.py holds every
parameter and the calculation, and its "Confidence" section gives
P(pass | perfect workbook) from the single judge FPR and the tier counts in
rubrics.csv. The FPR is a placeholder constant in that box.

By default draws leaderboard_v2.pdf under operation/results/v2/plots/leaderboard/
(a .png alongside under --png), at the printed width (style_guide.yaml `print`)
so nothing is scaled in the paper. Pass rate only: columns for the cohorts that
pass at least once, the 0% cohorts listed right of a rule, stacked by vendor
behind column-style swatches; the freed width lets the names tilt only as far as
they must, and the legend sits above the panel.

--v1 draws every cohort as a column instead, saved as leaderboard:

  column   the cohort's pass rate with a standard-error whisker, in the model
           vendor's hue (style_guide.yaml `company`) with a squircle cap
  harness  rides on the column itself, two ways at once: a hatch drawn in the
           surface colour (GUI flat, Excel diagonal, Code crossed, In-house
           dotted) and a tint of the vendor hue toward the surface
           (style_guide.yaml `harness_tint`, GUI fullest)
  legend   two rows in the panel's empty top-right corner, vendors then
           harnesses, the harness swatches on a neutral face so the hatch
           reads as a pattern

--with-score draws the v1 layout with the composite-score panel above the pass
rate (mean, whisker, "n/N" in slate when the cohort is not fully graded), saved
as leaderboard_with_score.

Cohorts are named in style_guide.yaml `agents` (display name -> vendor and the
database agent_model_names pooled into it, the newest grading kept when two of
them cover a task); an agent_model_name that is not there is skipped and listed
on stderr, so a new cohort has to be registered there before it appears.
Run with the shared plotting environment, ~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_leaderboard.py
    python operation/v2/paper_scripts/plot_leaderboard.py --png
    python operation/v2/paper_scripts/plot_leaderboard.py --with-score
    python operation/v2/paper_scripts/plot_leaderboard.py --v1
    python operation/v2/paper_scripts/plot_leaderboard.py --min-tasks 80 --plot-dir /tmp/plots
    python operation/v2/paper_scripts/plot_leaderboard.py --database-url postgresql://...
"""

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.transforms as mtransforms
import numpy as np
import psycopg2
import psycopg2.extras
from matplotlib.legend_handler import HandlerPatch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    BAR_ROUND_PT,
    BASELINE,
    INK,
    LEGEND_HANDLE_ROUND_PT,
    REPO_ROOT,
    STYLE,
    SURFACE,
    UNKNOWN_COLOR,
    _squircle_end_path,
    axis_title,
    database_url,
    quiet_axes,
    tick_top,
)
from pass_rule import (  # noqa: E402  the Pass Rate Derivation box
    DETERMINISTIC_CHECKS,
    FPR,
    TIER_TOLERANCE,
    TIERS,
    PassVerdict,
    confidence,
    load_tiers,
    pass_rate,
    task_verdict,
    tier_counts,
)

PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "leaderboard"
PLOT_NAME = "leaderboard"
MIN_JUDGE_VERSION = (
    12  # the current judge; earlier versions graded a different check set
)
MIN_TASKS_DEFAULT = 30  # cohorts graded on fewer tasks are still in flight
# Cohorts whose ungraded live tasks count as score 0 and a failed pass, so they sit on the full task set.
BACKFILL_ZERO = {"Fable 5.1", "Qwen 3.8", "Qwen 3.8-Code"}

COMPANY_COLORS: dict[str, str] = STYLE["company"]
HARNESS_TINT: dict[str, float] = STYLE["harness_tint"]
SLATE = STYLE["ink"]["slate"]

# task_attempts.agent_model_type -> harness name. Fixed order for the legend.
HARNESS = {"gui": "GUI", "excel": "Excel", "coding_cli": "Code", "api": "In-house"}
HARNESS_ORDER = ["GUI", "Code", "Excel", "In-house"]
# Hatch per harness, drawn in the surface colour over the tinted fill. GUI is flat
# and full-strength; each step away from it adds pattern and loses saturation.
HARNESS_HATCH = {"GUI": "", "Excel": "/", "Code": "x", "In-house": "."}
HATCH_DENSITY = 4# hatch repetitions per inch, on the columns and the legend swatches alike
HATCH_LW = 0.6  # points; matplotlib has one hatch line width per figure
HATCH_ALPHA = 0.6  # the hatch is the surface colour at this opacity, so it stays quiet
LEGEND_HARNESS_FACE = SLATE  # neutral face so the legend hatch reads as a pattern

# display name -> (vendor of the model, database agent_model_names), from style_guide.yaml `agents`.
# Only these are plotted.
COHORTS: dict[str, tuple[str, tuple[str, ...]]] = {
    name: (v["vendor"], tuple(v["db"])) for name, v in STYLE["agents"].items()
}
# every registered agent_model_name -> (display name, vendor)
AGENTS: dict[str, tuple[str, str]] = {}
for _name, (_vendor, _dbs) in COHORTS.items():
    for _db in _dbs:
        if _db in AGENTS:
            sys.exit(
                f"style_guide.yaml agents: {_db!r} is under both {AGENTS[_db][0]!r} and {_name!r}"
            )
        AGENTS[_db] = (_name, _vendor)


def db_names(names) -> list[str]:
    """Every agent_model_name of the given display names, for a SQL filter."""
    return [db for name in names for db in COHORTS[name][1]]


def fold_cohorts(rows: list[dict]) -> list[dict]:
    """One row per (cohort, task), tagged with its display name as `cohort` (None when unregistered).

    A cohort's agent_model_names are pooled; when two cover a task the newest grading (graded_at) wins.
    """
    newest: dict[tuple[str, int], dict] = {}
    for r in rows:
        name = AGENTS.get(r["agent_model_name"], (None,))[0]
        slot = (name or r["agent_model_name"], r["task_id"])
        if slot not in newest or r["graded_at"] > newest[slot]["graded_at"]:
            newest[slot] = {**r, "cohort": name}
    return list(newest.values())


# ============================================================================
# FIGURE STYLE
# ============================================================================
# The canvas is the printed size (style_guide.yaml print.textwidth_in, included with
# width=\textwidth), so every font size below is the size that prints. The tilted
# cohort names hang left of and below the axes; both margins are measured from the
# rendered names, and the columns share whatever width is left.
PRINT = STYLE["print"]
FIG_W_IN = PRINT["textwidth_in"]
SAVE_PAD_IN = PRINT["save_pad_in"]
SAVE_DPI = 300

PASS_AXES_H_IN = 1.2  # the pass-rate panel
SCORE_AXES_H_IN = 2.0  # the composite-score panel above it, under --with-score
PANEL_GAP_IN = 0.2  # between the score baseline and the pass panel's top tick
LEGEND_ROW_IN = 0.15  # one legend row; vendor and harness stack in two, in a band above the top panel
LEGEND_TITLE_PAD_PT = 10  # "Vendor" / "Harness" sit this far left of their row's first swatch
LEGEND_MAX_COLS = 8  # vendors past this wrap onto another row, so the legend stays within the print width
YLABEL_IN = 0.42  # y-axis title plus tick numbers
RIGHT_IN = 0.04
NAME_PAD_IN = 0.06  # below the deepest tilted name
X_PAD = 0.6  # columns of empty space before the first and after the last column

NAME_FS = PRINT["pt"]["label"]
VALUE_FS = PRINT["pt"]["label"]
COVER_FS = PRINT["pt"]["min"]
LEGEND_FS = PRINT["pt"]["label"]
LEGEND_WEIGHT = PRINT["legend_weight"]  # legend entries in Avenir Medium

BAR_W = 0.68  # column pitch is 1
NAME_ROTATION = (
    40  # degrees; steeper than the wide figure so the names hang less far left
)
SE_LW = 0.8
SE_CAP_PT = 2.0
VALUE_PAD_FRAC = (
    0.012  # gap between whisker and value label, as a share of the panel's y-range
)
YLIM = (0, 100)
YTICKS = (0, 20, 40, 60, 80, 100)
PASS_YTICK_STEP = (
    5  # percent; doubles until the pass panel has at most PASS_MAX_TICKS ticks
)
PASS_MAX_TICKS = 6
LEGEND_HANDLE_LENGTH = 1.6  # swatch size in font sizes, wide and tall enough to carry a few hatch repeats
LEGEND_HANDLE_HEIGHT = 1.1
LEGEND_COL_SPACING = 0.8  # between entries, in font sizes; with the swatch, keeps seven vendors on one row
LEGEND_TEXT_PAD = 0.3  # swatch to label, in font sizes

# --v2: the 0% cohorts leave the columns for a list right of a rule
PLOT_NAME_V2 = "leaderboard_v2"
NAME_LINE_GAP = 1.3  # tilted names clear their neighbours by this many font sizes
SEP_LW = 0.8  # the rule between the columns and the 0% list
ZERO_GAP_IN = 0.1  # rule to the 0% list
ZERO_HEADER = "0% pass rate"
ZERO_LINE_PT = 11  # one name in the 0% list
ZERO_GROUP_GAP_PT = 3  # extra space between vendors in the 0% list
ZERO_SWATCH_PAD_PT = 4  # swatch to name
ZERO_SWATCH = (1.6, 0.8)  # list swatch length and height in font sizes, a size under the legend's
V2_PASS_HEADROOM = 1.15  # the legend sits above the panel, so only the value labels need room
V2_LEGEND_GAP_IN = 0.06  # legend rows to the panel's top tick

plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["hatch.linewidth"] = HATCH_LW

SQL = """
    select distinct on (ta.agent_model_name, ta.task_id)
           ta.agent_model_name, ta.agent_model_type, ta.task_id,
           (g.scored_results->>'total_score')::float as score,
           g.scored_results->'check_scores' as check_scores, g.created_at as graded_at
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
    scores: list[float]
    verdicts: list[PassVerdict] = field(
        default_factory=list
    )  # tasks whose pass could be decided

    @property
    def n(self) -> int:
        return len(self.scores)

    @property
    def mean(self) -> float:
        return float(np.mean(self.scores))

    @property
    def se(self) -> float:
        return (
            float(np.std(self.scores, ddof=1) / np.sqrt(self.n)) if self.n > 1 else 0.0
        )

    @property
    def pass_n(self) -> int:
        return len(self.verdicts)

    @property
    def pass_rate(self) -> float:
        return pass_rate(self.verdicts)[0]

    @property
    def pass_se(self) -> float:
        return pass_rate(self.verdicts)[1]

    @property
    def color(self) -> str:
        """Vendor hue, mixed toward the surface by the harness tint."""
        base = COMPANY_COLORS.get(self.company, UNKNOWN_COLOR)
        t = HARNESS_TINT.get(self.harness, 0.0)
        rgb = (1 - t) * np.array(mcolors.to_rgb(base)) + t * np.array(
            mcolors.to_rgb(SURFACE)
        )
        return mcolors.to_hex(rgb)


# --- data ----------------------------------------------------------------------


def fetch(
    conn, min_tasks: int, tiers: dict[tuple[str, str], int]
) -> tuple[list[Cohort], int]:
    """Plotted cohorts (best first) and the size of the live task set."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select count(*) as n from tasks where deprecated is not true")
    n_tasks = cur.fetchone()["n"]
    cur.execute(SQL, {"judge": MIN_JUDGE_VERSION})
    rows = fold_cohorts(cur.fetchall())

    by_name: dict[str, Cohort] = {}
    unknown: dict[str, int] = {}
    undecided: dict[str, int] = {}
    for r in rows:
        name = r["cohort"]
        if name is None:
            unknown[r["agent_model_name"]] = unknown.get(r["agent_model_name"], 0) + 1
            continue
        if name not in by_name:
            company, dbs = COHORTS[name]
            by_name[name] = Cohort(
                dbs[0], name, company, HARNESS.get(r["agent_model_type"], "?"), []
            )
        by_name[name].scores.append(r["score"])
        verdict = task_verdict(r["check_scores"], tiers)
        if verdict is None:
            undecided[name] = undecided.get(name, 0) + 1
        else:
            by_name[name].verdicts.append(verdict)

    cur.execute("select id from tasks where deprecated is not true")
    live = {r["id"] for r in cur.fetchall()}
    for name in BACKFILL_ZERO & by_name.keys():
        missing = live - {r["task_id"] for r in rows if r["cohort"] == name}
        if missing:
            print(f"{name}: {len(missing)} ungraded tasks backfilled as 0 and failed", file=sys.stderr)
        # no workbook: every tier over its allowance, so the per-tier ok% in report() counts it failed too
        over = {t: TIER_TOLERANCE[t] + 1 for t in TIERS}
        for _ in missing:
            by_name[name].scores.append(0.0)
            by_name[name].verdicts.append(PassVerdict(False, False, over, {t: 0 for t in TIERS}))

    for key, n in sorted(unknown.items()):
        print(
            f"skipping {key}: {n} graded tasks but not in style_guide.yaml agents",
            file=sys.stderr,
        )
    for name, n in sorted(undecided.items()):
        print(
            f"{name}: {n} graded tasks without per-check verdicts or with an unscored "
            f"critical check; left out of the pass rate",
            file=sys.stderr,
        )
    for c in by_name.values():
        if c.n < min_tasks:
            print(
                f"skipping {c.name}: {c.n} < {min_tasks} graded tasks", file=sys.stderr
            )
        if c.company not in COMPANY_COLORS:
            print(
                f"{c.name}: vendor {c.company!r} has no colour in style_guide.yaml",
                file=sys.stderr,
            )
    return [c for c in by_name.values() if c.n >= min_tasks], n_tasks


def ranked(cohorts: list[Cohort], with_score: bool) -> list[Cohort]:
    """Best first: by pass rate when that is the only panel, by composite score when both show."""
    if with_score:
        return sorted(cohorts, key=lambda c: -c.mean)
    return sorted(cohorts, key=lambda c: (-c.pass_rate, -c.mean))


# --- figure ----------------------------------------------------------------------


def layout(cohorts: list[Cohort]) -> list[tuple[float, Cohort]]:
    """Column x per cohort in the given order, one pitch apart."""
    return [(float(i), c) for i, c in enumerate(cohorts)]


def squircle_column(ax, x: float, height: float, **style) -> mpatches.Polygon:
    """A column whose two top corners are squircled, drawn as one patch so a hatch covers it.

    The axes' limits must be final: the point radius is converted through transData.
    """
    x0, x1 = x - BAR_W / 2, x + BAR_W / 2
    to_px, to_data = ax.transData, ax.transData.inverted()
    (px0, py0), (px1, py1) = to_px.transform([(x0, 0), (x1, height)])
    r = min(BAR_ROUND_PT * ax.figure.dpi / 72.0, (px1 - px0) / 2)
    pts = to_data.transform(_squircle_end_path(px0, py0, px1, py1, r, horizontal=False))
    patch = mpatches.Polygon(pts, closed=True, **style)
    ax.add_patch(patch)
    return patch


class _HatchedHandler(HandlerPatch):
    """Legend swatch: rounded, carrying the handle's face colour and hatch."""

    def create_artists(
        self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans
    ):
        patch = mpatches.FancyBboxPatch(
            (-xdescent, -ydescent),
            width,
            height,
            boxstyle=f"round,pad=0,rounding_size={LEGEND_HANDLE_ROUND_PT}",
            facecolor=orig_handle.get_facecolor(),
            edgecolor=orig_handle.get_edgecolor(),
            hatch=orig_handle.get_hatch(),
            linewidth=0,
        )
        patch.set_transform(trans)
        return [patch]


def text_width_in(fig: plt.Figure, s: str, fontsize: float) -> float:
    """Rendered width of a string, in inches, at the figure's dpi."""
    probe = fig.text(0, 0, s, fontsize=fontsize)
    w = probe.get_window_extent(fig.canvas.get_renderer()).width / fig.dpi
    probe.remove()
    return w


def draw_columns(
    ax,
    cols: list[tuple[float, Cohort]],
    values: list[tuple[float, float, str, str | None]],
    hatch_color,
) -> None:
    """Squircle columns with whiskers and labels: (height, se, label, muted note) per column.

    A column of height 0 draws nothing but its label; the name underneath carries its identity.
    """
    pad = VALUE_PAD_FRAC * (ax.get_ylim()[1] - ax.get_ylim()[0])
    for (x, c), (h, se, label, note) in zip(cols, values):
        if h > 0:
            squircle_column(
                ax,
                x,
                h,
                facecolor=c.color,
                edgecolor=hatch_color,
                linewidth=0,
                hatch=HARNESS_HATCH.get(c.harness, "") * HATCH_DENSITY,
                zorder=2,
            )
        if se > 0:
            ax.errorbar(
                x,
                h,
                yerr=se,
                fmt="none",
                ecolor=INK,
                elinewidth=SE_LW,
                capsize=SE_CAP_PT,
                capthick=SE_LW,
                zorder=4,
            )
        t = ax.text(
            x,
            h + se + pad,
            label,
            ha="center",
            va="bottom",
            fontsize=VALUE_FS,
            color=INK,
            zorder=4,
        )
        if note:
            ax.annotate(
                note,
                xy=(0.5, 1),
                xycoords=t,
                xytext=(0, 1),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=COVER_FS,
                color=SLATE,
            )


def make_figure(cohorts: list[Cohort], n_tasks: int, with_score: bool) -> plt.Figure:
    cols = layout(cohorts)
    x_last = cols[-1][0]
    n_pitch = x_last + 2 * X_PAD  # axes width in column pitches
    fig = plt.figure(
        figsize=(FIG_W_IN, 1.0), facecolor=SURFACE
    )  # height follows the margins

    # margins from the rendered names: each hangs w*cos(theta) left and w*sin(theta) below
    # its column; the left one competes with the y-axis title, and the axes take the rest
    theta = math.radians(NAME_ROTATION)
    widths = [(x, text_width_in(fig, c.name, NAME_FS)) for x, c in cols]
    left_in = YLABEL_IN
    for _ in range(
        3
    ):  # the spill depends on the column pitch, which depends on the spill
        pitch_in = (FIG_W_IN - left_in - RIGHT_IN) / n_pitch
        spill = max(w * math.cos(theta) - (x + X_PAD) * pitch_in for x, w in widths)
        left_in = max(YLABEL_IN, spill + NAME_PAD_IN)
    axes_w_in = FIG_W_IN - left_in - RIGHT_IN
    bottom_in = max(w * math.sin(theta) for _, w in widths) + NAME_PAD_IN
    legend_in = legend_band_in(cohorts)  # the legend band above the top panel
    fig_h = (
        legend_in
        + (SCORE_AXES_H_IN + PANEL_GAP_IN if with_score else 0)
        + PASS_AXES_H_IN
        + bottom_in
    )
    fig.set_size_inches(FIG_W_IN, fig_h)
    x0, w = left_in / FIG_W_IN, axes_w_in / FIG_W_IN
    axp = fig.add_axes([x0, bottom_in / fig_h, w, PASS_AXES_H_IN / fig_h])
    ax = None
    if with_score:
        score_bottom_in = bottom_in + PASS_AXES_H_IN + PANEL_GAP_IN
        ax = fig.add_axes([x0, score_bottom_in / fig_h, w, SCORE_AXES_H_IN / fig_h])
    xlim = (-X_PAD, x_last + X_PAD)
    hatch_color = mcolors.to_rgba(SURFACE, HATCH_ALPHA)

    # pass panel: top on the first tick above the tallest whisker, names underneath
    pass_pct = [(100 * c.pass_rate, 100 * c.pass_se) for _, c in cols]
    tallest = max(p + s for p, s in pass_pct)
    pass_top, tick_step = tick_top(
        tallest * V2_PASS_HEADROOM, PASS_YTICK_STEP, PASS_MAX_TICKS
    )
    quiet_axes(axp)
    axp.set_xlim(*xlim)
    axp.set_ylim(0, pass_top)
    axp.set_yticks(list(range(0, pass_top + 1, tick_step)))
    axis_title(axp, "Pass rate (%)")
    axp.set_xticks(
        [x for x, _ in cols],
        [c.name for _, c in cols],
        fontsize=NAME_FS,
        color=INK,
        rotation=NAME_ROTATION,
        ha="right",
        rotation_mode="anchor",
    )
    draw_columns(
        axp, cols, [(p, s, f"{p:.0f}%", None) for p, s in pass_pct], hatch_color
    )

    # score panel under --with-score: same columns, names already carried by the pass panel
    if with_score:
        quiet_axes(ax)
        ax.set_xlim(*xlim)
        ax.set_ylim(*YLIM)
        ax.set_yticks(list(YTICKS))
        axis_title(ax, "Composite score (0-100)")
        ax.set_xticks([])
        draw_columns(
            ax,
            cols,
            [
                (
                    c.mean,
                    c.se,
                    f"{c.mean:.1f}",
                    f"{c.n}/{n_tasks}" if c.n < n_tasks else None,
                )
                for _, c in cols
            ],
            hatch_color,
        )

    top_ax, top_h_in = (ax, SCORE_AXES_H_IN) if with_score else (axp, PASS_AXES_H_IN)
    draw_legend(top_ax, cohorts, hatch_color, top=1 + legend_in / top_h_in)
    return fig


def vendor_order(cohorts: list[Cohort]) -> list[str]:
    """Vendors in order of their first cohort."""
    vendors = []
    for c in cohorts:
        if c.company not in vendors:
            vendors.append(c.company)
    return vendors


def legend_rows(cohorts: list[Cohort]) -> int:
    """Rows the legend takes: the vendors wrapped at LEGEND_MAX_COLS, then one for the harnesses."""
    return math.ceil(len(vendor_order(cohorts)) / LEGEND_MAX_COLS) + 1


def legend_band_in(cohorts: list[Cohort]) -> float:
    """Height of the legend band above the top panel: its rows plus the gap to the panel's top tick."""
    return legend_rows(cohorts) * LEGEND_ROW_IN + V2_LEGEND_GAP_IN


def _row_major(handles: list, ncol: int) -> list:
    """Reorder handles so matplotlib's column-major fill reads left to right, row by row."""
    nrow = math.ceil(len(handles) / ncol)
    return [handles[r * ncol + c] for c in range(ncol) for r in range(nrow) if r * ncol + c < len(handles)]


def draw_legend(axp, cohorts: list[Cohort], hatch_color, right: float = 1.0, top: float = 1.0) -> None:
    """Legend rows, vendors (wrapped at LEGEND_MAX_COLS, columns aligned) above harnesses, right-aligned
    at `right` and hanging from `top` (fractions of `axp`; the callers pass a band above the panel, sized by
    legend_band_in, so the key never sits on the data). Each group is titled just left of its first row
    once its width is known."""
    fig = axp.figure
    v_handles = [
        mpatches.Patch(
            facecolor=COMPANY_COLORS.get(v, UNKNOWN_COLOR), edgecolor="none", label=v
        )
        for v in vendor_order(cohorts)
    ]
    harnesses = [h for h in HARNESS_ORDER if any(c.harness == h for c in cohorts)]
    h_handles = [
        mpatches.Patch(
            facecolor=_tinted(LEGEND_HARNESS_FACE, h),
            edgecolor=hatch_color,
            hatch=HARNESS_HATCH[h] * HATCH_DENSITY,
            label=h,
        )
        for h in harnesses
    ]
    common = dict(
        frameon=False,
        prop={"size": LEGEND_FS, "weight": LEGEND_WEIGHT},
        labelcolor=INK,
        handlelength=LEGEND_HANDLE_LENGTH,
        handleheight=LEGEND_HANDLE_HEIGHT,
        columnspacing=LEGEND_COL_SPACING,
        handletextpad=LEGEND_TEXT_PAD,
        borderpad=0,
        borderaxespad=0,
        handler_map={mpatches.Patch: _HatchedHandler()},
    )
    row_frac = LEGEND_ROW_IN / (axp.get_position().height * fig.get_figheight())  # one row, as a fraction of the panel
    rows_above = 0
    for title, handles in (("Vendor", v_handles), ("Harness", h_handles)):
        ncol = min(len(handles), LEGEND_MAX_COLS)
        nrow = math.ceil(len(handles) / ncol)
        y = top - (rows_above + nrow / 2) * row_frac
        rows_above += nrow
        leg = axp.legend(
            handles=_row_major(handles, ncol),
            loc="center right",
            ncol=ncol,
            bbox_to_anchor=(right, y),
            labelspacing=(LEGEND_ROW_IN * 72 - LEGEND_FS * LEGEND_HANDLE_HEIGHT) / LEGEND_FS,
            **common,
        )
        axp.add_artist(leg)  # a second axes.legend() call would replace the first
        fig.canvas.draw()  # the row's extent is only known once drawn
        # the title lines up with the entry labels' centre, not the row's, which the swatches skew
        label = leg.get_texts()[0].get_window_extent()
        left, y_label = axp.transAxes.inverted().transform(
            (leg.get_window_extent().x0, (label.y0 + label.y1) / 2)
        )
        axp.annotate(
            title,
            xy=(left, y_label),
            xycoords="axes fraction",
            xytext=(-LEGEND_TITLE_PAD_PT, 0),
            textcoords="offset points",
            ha="right",
            va="center",
            fontsize=LEGEND_FS,
            color=SLATE,
            annotation_clip=False,
        )


def name_tilt(pitch_in: float, fontsize: float) -> float:
    """Radians: the flattest tilt at which names one pitch apart sit NAME_LINE_GAP font sizes apart."""
    return math.asin(min(1.0, NAME_LINE_GAP * fontsize / 72 / pitch_in))


def make_figure_v2(cohorts: list[Cohort]) -> plt.Figure:
    """Columns for the cohorts that pass at least once; the 0% cohorts listed right of a rule.

    Dropping the 0% columns leaves the rest a wider pitch, so the names tilt only as far as
    they must to clear each other (NAME_LINE_GAP). The list stacks the 0% cohorts by vendor,
    in legend order, each behind a swatch styled like its column would be.
    """
    shown = [c for c in cohorts if c.pass_rate > 0]
    zero = [c for c in cohorts if c.pass_rate == 0]
    groups = [g for v in vendor_order(cohorts) if (g := [c for c in zero if c.company == v])]
    fig = plt.figure(figsize=(FIG_W_IN, 1.0), facecolor=SURFACE)

    swatch_w_in, swatch_h_in = (s * NAME_FS / 72 for s in ZERO_SWATCH)
    name_x_in = swatch_w_in + ZERO_SWATCH_PAD_PT / 72
    list_w_in = max(
        [text_width_in(fig, ZERO_HEADER, LEGEND_FS)]
        + [name_x_in + text_width_in(fig, c.name, NAME_FS) for c in zero]
    )
    line_in = ZERO_LINE_PT / 72
    list_h_in = line_in * (1 + len(zero)) + ZERO_GROUP_GAP_PT / 72 * (len(groups) - 1)
    right_in = RIGHT_IN + (ZERO_GAP_IN + list_w_in if zero else 0)

    # names tilt just enough to clear each other (name_tilt); the tilt, the left spill
    # and the pitch depend on each other, so settle them together
    cols = layout(shown)
    x_last = cols[-1][0]
    n_pitch = x_last + 2 * X_PAD
    widths = [(x, text_width_in(fig, c.name, NAME_FS)) for x, c in cols]
    left_in = YLABEL_IN
    for _ in range(5):
        pitch_in = (FIG_W_IN - left_in - right_in) / n_pitch
        theta = name_tilt(pitch_in, NAME_FS)
        spill = max(w * math.cos(theta) - (x + X_PAD) * pitch_in for x, w in widths)
        left_in = max(YLABEL_IN, spill + NAME_PAD_IN)
    axes_w_in = FIG_W_IN - left_in - right_in
    bottom_in = max(
        max(w * math.sin(theta) for _, w in widths) + NAME_PAD_IN,
        list_h_in - PASS_AXES_H_IN,
    )
    legend_in = legend_band_in(cohorts)
    fig_h = legend_in + PASS_AXES_H_IN + bottom_in
    fig.set_size_inches(FIG_W_IN, fig_h)
    axp = fig.add_axes(
        [left_in / FIG_W_IN, bottom_in / fig_h, axes_w_in / FIG_W_IN, PASS_AXES_H_IN / fig_h]
    )
    hatch_color = mcolors.to_rgba(SURFACE, HATCH_ALPHA)

    pass_pct = [(100 * c.pass_rate, 100 * c.pass_se) for _, c in cols]
    tallest = max(p + s for p, s in pass_pct)
    pass_top, tick_step = tick_top(tallest * V2_PASS_HEADROOM, PASS_YTICK_STEP, PASS_MAX_TICKS)
    quiet_axes(axp)
    axp.set_xlim(-X_PAD, x_last + X_PAD)
    axp.set_ylim(0, pass_top)
    axp.set_yticks(list(range(0, pass_top + 1, tick_step)))
    axis_title(axp, "Pass rate (%)")
    axp.set_xticks(
        [x for x, _ in cols],
        [c.name for _, c in cols],
        fontsize=NAME_FS,
        color=INK,
        rotation=math.degrees(theta),
        ha="right",
        rotation_mode="anchor",
    )
    draw_columns(axp, cols, [(p, s, f"{p:.0f}%", None) for p, s in pass_pct], hatch_color)
    # legend band above the panel, right-aligned with the figure so it spans the list too
    draw_legend(
        axp,
        cohorts,
        hatch_color,
        right=(FIG_W_IN - RIGHT_IN - left_in) / axes_w_in,
        top=1 + legend_in / PASS_AXES_H_IN,
    )
    if not zero:
        return fig

    # the rule closes the panel on the right, top tick to baseline; the list hangs from the top beside it
    # inches from the bottom left, through transFigure: bbox_inches="tight" moves that one, not dpi_scale_trans
    inches = mtransforms.Affine2D().scale(1 / FIG_W_IN, 1 / fig_h) + fig.transFigure
    top_in = bottom_in + PASS_AXES_H_IN
    x_sep = left_in + axes_w_in
    fig.add_artist(
        plt.Line2D(
            [x_sep, x_sep],
            [bottom_in, top_in],
            transform=inches,
            color=BASELINE,
            linewidth=SEP_LW,
        )
    )
    x0 = x_sep + ZERO_GAP_IN
    fig.text(x0, top_in - line_in / 2, ZERO_HEADER, transform=inches, ha="left", va="center",
             fontsize=LEGEND_FS, color=INK, fontweight=LEGEND_WEIGHT)
    y = top_in - 1.5 * line_in
    for gi, group in enumerate(groups):
        if gi:
            y -= ZERO_GROUP_GAP_PT / 72
        for c in group:
            fig.add_artist(
                mpatches.FancyBboxPatch(
                    (x0, y - swatch_h_in / 2),
                    swatch_w_in,
                    swatch_h_in,
                    boxstyle=f"round,pad=0,rounding_size={LEGEND_HANDLE_ROUND_PT / 72}",
                    transform=inches,
                    facecolor=c.color,
                    edgecolor=hatch_color,
                    hatch=HARNESS_HATCH.get(c.harness, "") * HATCH_DENSITY,
                    linewidth=0,
                )
            )
            fig.text(x0 + name_x_in, y, c.name, transform=inches, ha="left", va="center",
                     fontsize=NAME_FS, color=INK)
            y -= line_in
    return fig


def _tinted(hex_color: str, harness: str) -> str:
    """The harness tint applied to an arbitrary face colour (legend swatches)."""
    t = HARNESS_TINT.get(harness, 0.0)
    rgb = (1 - t) * np.array(mcolors.to_rgb(hex_color)) + t * np.array(
        mcolors.to_rgb(SURFACE)
    )
    return mcolors.to_hex(rgb)


def save_figure(
    fig: plt.Figure, plot_dir: Path, name: str, png: bool = False
) -> list[Path]:
    """<name>.pdf, the paper's copy, plus a <name>.png preview when asked."""
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("pdf", "png") if png else ("pdf",):
        path = plot_dir / f"{name}.{ext}"
        fig.savefig(
            path,
            dpi=SAVE_DPI,
            bbox_inches="tight",
            pad_inches=SAVE_PAD_IN,
            facecolor=SURFACE,
        )
        paths.append(path)
    return paths


def report(
    cohorts: list[Cohort], n_tasks: int, tiers: dict[tuple[str, str], int]
) -> None:
    print(
        f"{len(cohorts)} cohorts, {n_tasks} live tasks, judge >= v{MIN_JUDGE_VERSION}"
    )
    counts = tier_counts(tiers)
    conf, per_tier = confidence(tiers)
    print(f"pass = flags_t <= k_t in every tier; FPR {FPR:.2%} (PLACEHOLDER)")
    for t in TIERS:
        print(
            f"  tier {t}: N={counts[t]:>3}  k={TIER_TOLERANCE[t]}  P(Bin(N, FPR) <= k) = {per_tier[t]:.3f}"
        )
    print(f"  P(pass | perfect workbook) = {conf:.3f}")
    print(
        f"  deterministic, must be unflagged (FPR 0): {', '.join(n for _, n in sorted(DETERMINISTIC_CHECKS))}"
    )
    print(
        f"det = % of tasks with no deterministic check flagged; ok_t = % of tasks within tier t's allowance"
    )
    ok_cols = "".join(f"{f'ok{t}%':>7}" for t in TIERS)
    print(
        f"{'#':>3}  {'cohort':<34}{'harness':<10}{'vendor':<11}{'n':>4}{'mean':>7}{'se':>6}"
        f"{'n_pass':>8}{'pass%':>7}{'se':>6}{'det%':>7}{ok_cols}"
    )
    for i, c in enumerate(cohorts, 1):
        ok = f"{100 * np.mean([v.deterministic_ok for v in c.verdicts]) if c.verdicts else 0.0:>7.1f}"
        ok += "".join(
            f"{100 * np.mean([v.flags[t] <= TIER_TOLERANCE[t] for v in c.verdicts]) if c.verdicts else 0.0:>7.1f}"
            for t in TIERS
        )
        print(
            f"{i:>3}  {c.name:<34}{c.harness:<10}{c.company:<11}{c.n:>4}{c.mean:>7.1f}{c.se:>6.1f}"
            f"{c.pass_n:>8}{100 * c.pass_rate:>7.1f}{100 * c.pass_se:>6.1f}{ok}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--database-url", help="full connection string; bypasses config/config.yaml"
    )
    parser.add_argument(
        "--min-tasks",
        type=int,
        default=MIN_TASKS_DEFAULT,
        help=f"drop cohorts graded on fewer tasks (default {MIN_TASKS_DEFAULT})",
    )
    parser.add_argument(
        "--with-score",
        action="store_true",
        help="add the composite-score panel above the pass rate",
    )
    parser.add_argument(
        "--v1",
        action="store_true",
        help=f"draw every cohort as a column instead of {PLOT_NAME_V2}, saved as {PLOT_NAME}",
    )
    parser.add_argument(
        "--png", action="store_true", help="also save a .png preview next to the .pdf"
    )
    parser.add_argument(
        "--plot-dir",
        type=Path,
        default=PLOT_DIR,
        help=f"directory the figure is saved to as {PLOT_NAME_V2}.pdf (default: {PLOT_DIR})",
    )
    args = parser.parse_args()

    tiers = load_tiers()
    conn = psycopg2.connect(database_url(args))
    try:
        cohorts, n_tasks = fetch(conn, args.min_tasks, tiers)
    finally:
        conn.close()
    if not cohorts:
        sys.exit("no cohort passes --min-tasks")
    cohorts = ranked(cohorts, args.with_score)
    report(cohorts, n_tasks, tiers)

    if args.v1 or args.with_score:  # the score panel exists only in the v1 layout
        fig = make_figure(cohorts, n_tasks, args.with_score)
        name = PLOT_NAME + ("_with_score" if args.with_score else "")
    else:
        fig, name = make_figure_v2(cohorts), PLOT_NAME_V2
    for path in save_figure(fig, args.plot_dir, name, png=args.png):
        print(f"wrote {path}")
    plt.close(fig)


if __name__ == "__main__":
    main()
