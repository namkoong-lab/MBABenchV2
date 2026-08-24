"""
Combined 2xN figure (v1 -- data-driven): performance and attempt time vs task
difficulty, with one column per harness group (API, and GUI + CODE together).

  Top row    -- composite (or component) score, 0-100
  Bottom row -- attempt time in minutes, log y by default

Reproduces BizbenchV1/judge/experiment_scripts/plot_performance_vs_time.py, but
both rows come from a single source: the run manifest written by
operation/v1/get_results.py (operation/results/v1/<timestamp>/v1_results.json).
The V1 script joined two separate JSONs whose (model, task) coverage could
differ; here every cell carries its own score and its own time_taken_min, so a
model's two panels describe exactly the same attempts.

The two rows do NOT share an imputation rule, and this is deliberate:

  scores  ungraded cells (needs_grading / needs_attempt) count as 0, matching
          get_results.py -- so a bucket's mean here equals the `total` in
          v1_results_difficulty_breakdown.json
  time    averaged over attempts that recorded a time_taken_min only. A missing
          attempt has no duration, and zero-filling would make the cohorts that
          ran least look fastest. Panel n's are printed per cohort at startup.

x ticks are difficulty ranks (1 = VERY-EASY ... 6 = VERY-HARD) over the buckets
actually present in the manifest.

Saves to ../../results/v1/plots/performance_vs_time/ relative to this file.
All adjustable items are at the top.

Needs matplotlib, numpy and scipy (PCHIP smoothing). Run it with a plotting
environment, not the repo venv -- the repo venv carries the DB/runner deps only.

Usage:
    python operation/v1/paper_scripts/plot_performance_vs_time.py
    python .../plot_performance_vs_time.py --metric accuracy
    python .../plot_performance_vs_time.py --statistic median
    python .../plot_performance_vs_time.py --time-y-scale linear --err-style bar
    python .../plot_performance_vs_time.py --shared-y
    python .../plot_performance_vs_time.py --results operation/results/v1/<ts>/v1_results.json
"""

import argparse
import colorsys
import json
import os
from collections import defaultdict

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.artist import Artist
from matplotlib.lines import Line2D
from matplotlib.transforms import IdentityTransform

# Hard import: --smooth is on by default, and a soft fallback would quietly
# turn every curve back into straight segments without saying so.
from scipy.interpolate import PchipInterpolator

# ============================================================================
# OUTPUT
# ============================================================================
_HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.normpath(
    os.path.join(_HERE, "../../results", "v1", "plots", "performance_vs_time")
)
SAVE_NAME = "performance_and_time_by_difficulty"
SAVE_DPI = 200

# ============================================================================
# DATA SOURCE
# ============================================================================
# Run manifests live in operation/results/<version>/<timestamp>/. With no
# --results argument the newest timestamp directory is used (they sort
# lexicographically because the stamp is YYYYMMDDTHHMMSSZ).
RESULTS_ROOT = os.path.normpath(os.path.join(_HERE, "../../results", "v1"))
RESULTS_FILENAME = "v1_results.json"

# Map get_results.py cohort label -> (display_name, family). Kept in sync with
# plot_main.py so the two figures name the same model the same way. The harness
# (api | gui | coding_cli) comes from the manifest, not from here. Cohorts
# present in this map and in the manifest are plotted; others are skipped.
COHORT_MAP: dict[str, tuple[str, str]] = {
    # --- coding CLI ---------------------------------------------------------
    "claude_code_fable_5": ("Claude Code (Fable 5)", "Anthropic"),
    "codex_sol_5.6": ("Codex (GPT-5.6)", "OpenAI"),
    # --- GUI ----------------------------------------------------------------
    "fable_5_gui": ("Claude Cowork (Fable 5)", "Anthropic"),
    "sol_5.6_gui": ("ChatGPT Work (GPT-5.6)", "OpenAI"),
    "claude_web": ("Claude Web (Opus 4.6)", "Anthropic"),
    "claude_excel": ("Claude Excel (Opus 4.6)", "Anthropic"),
    "chatgpt_pro": ("ChatGPT Pro (GPT-5.4)", "OpenAI"),
    "chatgpt_agent": ("ChatGPT Agent", "OpenAI"),
    "chatgpt_excel": ("ChatGPT Excel (GPT-5.4)", "OpenAI"),
    # --- API ----------------------------------------------------------------
    "fable_5_cli": ("Fable 5", "Anthropic"),
    "sol_5.6_cli": ("GPT-5.6 Sol", "OpenAI"),
    "opus_4_6": ("Opus 4.6", "Anthropic"),
    "gpt_5_4": ("GPT-5.4", "OpenAI"),
    "gemini_3_1_pro": ("Gemini 3.1 Pro", "Google"),
    "grok_4_2": ("Grok 4.2", "xAI"),
    "kimi_k2_5": ("Kimi K2.5", "Moonshot"),
    "qwen_3_6": ("Qwen 3.6+", "Alibaba"),
    "olmo_3_1": ("OLMo 3.1", "AI2"),
}

# Figure columns: (key, panel title, manifest pipelines drawn in it). A column
# may hold more than one pipeline -- GUI and CODE share one because both
# drive a real application end to end, which is the contrast against the API
# column. Cohorts inside a column are ordered by the pipeline order listed here,
# then by COHORT_MAP order. A column whose pipelines are all absent from the
# manifest is dropped and the grid narrows.
COLUMNS: list[tuple[str, str, list[str]]] = [
    ("api", "API", ["api"]),
    ("agentic", "GUI + CODE", ["gui", "coding_cli"]),
]

# Within a merged column the harness is carried by the line style, so a dashed
# line reads as CODE without needing its own legend block. Pipelines not
# listed here draw solid.
PIPELINE_LINESTYLES: dict[str, object] = {"coding_cli": (0, (5.5, 1.8))}

# ============================================================================
# DIFFICULTY AXIS
# ============================================================================
# tasks.human_difficulty_measure buckets, easiest first -- must match
# DIFFICULTY_ORDER in get_results.py. Buckets absent from the manifest are
# dropped; rank labels stay tied to this list, so dropping VERY-HARD leaves
# the rest as 1..5 rather than renumbering them.
DIFFICULTY_ORDER = ["VERY-EASY", "EASY", "MEDIUM", "MEDIUM-HARD", "HARD", "VERY-HARD"]
DIFFICULTY_LABELS = {name: str(i + 1) for i, name in enumerate(DIFFICULTY_ORDER)}

# Buckets hidden by default. VERY-HARD is a handful of tasks at most, so its
# per-model means are too noisy to draw as a trend point. --include-all-buckets
# puts it back.
EXCLUDED_BUCKETS = ["VERY-HARD"]

METRIC_CHOICES = ["total", "accuracy", "formula", "format"]
METRIC_DISPLAY = {
    "total": "composite score",
    "accuracy": "accuracy score",
    "formula": "formula score",
    "format": "format score",
}

# Number of interpolated samples between knots when smoothing is on.
SMOOTH_SAMPLES = 200

# ============================================================================
# GLOBAL STYLE -- mirrors plot_main.py.
# ============================================================================
EMBED_FONTS_IN_PDF = True
AXES_LINEWIDTH = 0.6

plt.rcParams.update(
    {
        "font.family": "Helvetica",
        "axes.unicode_minus": False,
        "mathtext.fontset": "stixsans",
        "axes.titleweight": "normal",
        "axes.labelcolor": "#000000",
        "xtick.color": "#000000",
        "ytick.color": "#000000",
        "axes.edgecolor": "#000000",
        "axes.linewidth": AXES_LINEWIDTH,
        "xtick.major.width": AXES_LINEWIDTH,
        "ytick.major.width": AXES_LINEWIDTH,
        "xtick.major.size": 2.5,
        "ytick.major.size": 2.5,
    }
)
if EMBED_FONTS_IN_PDF:
    plt.rcParams["pdf.fonttype"] = 42
    plt.rcParams["ps.fonttype"] = 42

MODEL_FAMILY_COLORS = {
    "AI2": "#0A3D62",
    "Alibaba": "#0040FF",
    "Moonshot": "#55B3C5",
    "OpenAI": "#000000",
    "xAI": "#1D9BF0",
    "Google": "#8B5CF6",
    "Anthropic": "#F25C1F",
}

# Cohorts whose brand color is their own, not a shade of the family ramp. The
# OpenAI family base is black and four OpenAI cohorts share the GUI panel, so
# the two product greens carry their own hue and leave the gray ramp to the
# rest.
COHORT_COLOR_OVERRIDES: dict[str, str] = {
    "chatgpt_agent": "#10A37F",
    "chatgpt_excel": "#1F6F73",
}

# Within-family lightness ramp, so same-family cohorts stay distinguishable.
# The merged GUI + CODE column puts 5 OpenAI and 4 Anthropic cohorts on
# one panel, so the ramp has to separate that many lines while keeping the
# palest one readable on white -- hence the lightness ceiling below.
FAMILY_LIGHTNESS_SPREAD = 0.34
FAMILY_DARK_THRESHOLD = 0.15
FAMILY_DARK_UP_RANGE = 0.45
FAMILY_LIGHTNESS_MAX = 0.72
FAMILY_LIGHTNESS_MIN = 0.05

TICK_FONTSIZE = 18
LABEL_FONTSIZE = 21
DIFFICULTY_TICK_FS = 19
PANEL_TITLE_FS = 22

FIG_SIZE = (16, 9.5)

LINE_WIDTH = 2.6
MARKER_SIZE = 7.5
MARKER_EDGE_WIDTH = 1.3
MARKER_EDGE_COLOR = "white"
LINE_ALPHA = 0.9
# Error-bar styling (--err-style bar)
ERR_CAPSIZE = 0
ERR_LINEWIDTH = 1.4
ERR_ALPHA = 0.85
# Shaded-band styling (--err-style band)
BAND_ALPHA = 0.16

# Inter-row legends -- one per column, placed in the gap between the
# performance and time rows. These are figure-level legends (fig.legend) rather
# than axes-level ones: an axes legend anchored outside its axes is accounted
# for by tight_layout, which would squeeze the panels. Figure legends float
# above the grid and leave it undistorted.
#
# LEGEND_GAP_FRAC is the subplot hspace that opens the gap; the legend's x/y is
# then measured off the drawn axes rather than hardcoded, so retuning the gap
# doesn't require retuning the legend position.
#
# Horizontally the legends are packed, not centered on their columns. Centering
# is what you want when the strips are narrow, but at this font size the two
# blocks together are wider than the panel grid, so centering both makes them
# collide in the middle and hang off the right edge. pack_legend_lefts() keeps
# each block as close to its column centre as it can while honouring the band
# edges and a minimum separation -- which in practice pushes the left block into
# the margin under the y tick labels (empty in this band) and pulls the right
# block back to the last panel's right edge.
LEGEND_GAP_FRAC = 0.46
LEGEND_FONTSIZE = 15.5
LEGEND_FRAMEON = True
LEGEND_FACECOLOR = "white"
LEGEND_EDGECOLOR = "none"
LEGEND_FRAMEALPHA = 0.35
# Padding/spacing are tight because the two blocks have to share one figure
# width at LEGEND_FONTSIZE; loosening them again means overflow unless the font
# comes back down.
LEGEND_BORDERPAD = 0.35
LEGEND_LABELSPACING = 0.35
LEGEND_HANDLELENGTH = 1.2
LEGEND_HANDLETEXTPAD = 0.4
LEGEND_HANDLE_LINEWIDTH = 6.0
LEGEND_COLUMNSPACING = 0.8
# Left edge of the band the legend strips are packed into, as a figure fraction.
# The right edge is measured off the last panel so the strip finishes flush with
# the grid. LEGEND_MIN_GAP is the whitespace kept between adjacent blocks.
LEGEND_BAND_LEFT = 0.02
# How far past the last panel's right edge the rightmost block may reach. The
# widest block gets pinned against this edge, so pushing it out is what frees
# room for the middle gap -- the left block then follows it rather than the
# blocks meeting in the middle.
LEGEND_BAND_RIGHT_OVERHANG = 0.05
# Separation between adjacent blocks. It is a floor, but with the right block
# pinned to the band edge the realised gap equals it exactly, so treat this as
# the knob for how far apart the two legends sit. For reference, the gap between
# columns *inside* a block is LEGEND_COLUMNSPACING * LEGEND_FONTSIZE, about
# 0.012 here -- this wants to be several times that to read as a break.
LEGEND_MIN_GAP = 0.07
# A rule drawn down the middle of each inter-block gap, so the break is marked
# rather than merely implied by whitespace.
#
# Both of its coordinates are measured off the drawn legends rather than the
# anchors used to place them. Horizontally that is because fig.legend insets the
# frame from its anchor by borderaxespad; vertically because a legend box is
# centred on its text *line boxes*, which carry unused ascent above the capitals
# and so sit higher than the rows look. Measuring the frames and the handle rows
# after the fact sidesteps both. LEGEND_DIVIDER_OVERHANG is how far past the
# first and last rows the rule runs, in units of the row pitch.
LEGEND_DIVIDER = True
LEGEND_DIVIDER_COLOR = "#BBBBBB"
LEGEND_DIVIDER_LINEWIDTH = 1.1
LEGEND_DIVIDER_OVERHANG = 0.55
# Swatch for the dashed (CODE) cohorts -- Claude Code and Codex. They keep the
# same round-capped bar as everyone else, just split into two stubs so the
# swatch says "this line is dashed in the panels" on its own. The gap below is
# the visible white between the two stubs; the stub length itself is solved for
# in legend_dash_pattern() so the pair spans exactly the same box as a solid
# swatch and both ends line up down the legend column.
LEGEND_DASHED_GAP = 4.0
# Entries per legend column; a panel with more cohorts than this wraps into a
# second (or third) legend column so the strip stays inside the gap.
LEGEND_MAX_ROWS = 4


# ============================================================================
# DATA LOADING
# ============================================================================
def latest_results_path(root: str = RESULTS_ROOT) -> str:
    """Newest operation/results/v1/<timestamp>/v1_results.json."""
    if not os.path.isdir(root):
        raise SystemExit(f"No results directory at {root}")
    runs = sorted(
        d
        for d in os.listdir(root)
        if os.path.isfile(os.path.join(root, d, RESULTS_FILENAME))
    )
    if not runs:
        raise SystemExit(f"No run directory under {root} holds {RESULTS_FILENAME}")
    return os.path.join(root, runs[-1], RESULTS_FILENAME)


def load_cells(path: str, metric: str):
    """Read the manifest into per-cohort score and time rows.

    Returns (score_rows, time_rows, pipelines):
      score_rows  {cohort: [(difficulty, value), ...]} with ungraded cells at 0
      time_rows   {cohort: [(difficulty, minutes), ...]} timed attempts only
      pipelines   {cohort: 'api' | 'gui' | 'coding_cli'} straight from the manifest
    """
    with open(path) as f:
        data = json.load(f)

    cells = data.get("cells", [])
    if not cells:
        raise SystemExit(f"No cells in {path}")
    if "time_taken_min" not in cells[0]:
        raise SystemExit(
            f"{path} has no time_taken_min on its cells -- it predates timing "
            "support in get_results.py. Re-run: python operation/v1/get_results.py"
        )

    score_rows: dict[str, list[tuple[str, float]]] = defaultdict(list)
    time_rows: dict[str, list[tuple[str, float]]] = defaultdict(list)
    pipelines: dict[str, str] = {}

    for c in cells:
        cohort = c["cohort"]
        if cohort not in COHORT_MAP:
            continue
        pipelines.setdefault(cohort, c["pipeline"])
        difficulty = c["difficulty"]
        if difficulty is None:
            continue
        # Scores impute 0 for cells without a clean grading (get_results.py's
        # rule); times are only real where an attempt recorded one.
        value = c[metric] if c["status"] == "complete" else 0.0
        score_rows[cohort].append((difficulty, float(value)))
        if c["time_taken_min"] is not None:
            time_rows[cohort].append((difficulty, float(c["time_taken_min"])))

    skipped = sorted({c["cohort"] for c in cells} - set(COHORT_MAP))
    if skipped:
        print(f"[plot_pvt] not in COHORT_MAP, skipped: {', '.join(skipped)}")

    return score_rows, time_rows, pipelines


# ============================================================================
# AGGREGATION
# ============================================================================
def aggregate(rows: dict[str, list[tuple[str, float]]], difficulties: list[str]):
    """{cohort: {difficulty: {mean, sem, median, q25, q75, n}}}."""
    by_cd: dict[tuple[str, str], list[float]] = defaultdict(list)
    for cohort, pairs in rows.items():
        for difficulty, value in pairs:
            if difficulty in difficulties:
                by_cd[(cohort, difficulty)].append(value)

    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for (cohort, difficulty), values in by_cd.items():
        arr = np.asarray(values, dtype=float)
        n = arr.size
        out[cohort][difficulty] = {
            "mean": float(arr.mean()),
            "sem": float(arr.std(ddof=1) / np.sqrt(n)) if n > 1 else 0.0,
            "median": float(np.median(arr)),
            "q25": float(np.quantile(arr, 0.25)),
            "q75": float(np.quantile(arr, 0.75)),
            "n": int(n),
        }
    return out


def center_and_err(cell: dict, statistic: str) -> tuple[float, float, float]:
    """(center, err_low, err_high) -- SEM around the mean, IQR around the median."""
    if statistic == "mean":
        return cell["mean"], cell["sem"], cell["sem"]
    if statistic == "median":
        c = cell["median"]
        return c, max(c - cell["q25"], 0.0), max(cell["q75"] - c, 0.0)
    raise ValueError(f"unknown statistic {statistic!r}")


def smooth_curve(xs, ys, n_out: int = SMOOTH_SAMPLES):
    """Monotone-cubic-Hermite (PCHIP) interpolation.

    PCHIP is shape-preserving -- it never overshoots past the local data
    extrema -- so a smoothed score band stays inside 0-100 without clipping and
    the line still passes through every marker. Two knots interpolate linearly;
    one knot is returned untouched.
    """
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if x.size < 2:
        return x, y
    x_dense = np.linspace(x[0], x[-1], n_out)
    if x.size == 2:
        return x_dense, np.interp(x_dense, x, y)
    return x_dense, PchipInterpolator(x, y)(x_dense)


# ============================================================================
# COLOR HELPERS
# ============================================================================
def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        *(max(0, min(255, int(round(v * 255)))) for v in (r, g, b))
    )


def vary_within_family(hex_color: str, idx: int, total: int) -> str:
    """Shade a family color so same-family cohorts in one panel stay apart."""
    if total <= 1:
        return hex_color
    r, g, b = _hex_to_rgb(hex_color)
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    if lightness < FAMILY_DARK_THRESHOLD:
        # Near-black bases (OpenAI) have no room to darken -- ramp upward only.
        new_l = lightness + (idx / (total - 1)) * FAMILY_DARK_UP_RANGE
    else:
        new_l = (
            lightness
            - FAMILY_LIGHTNESS_SPREAD / 2
            + (idx / (total - 1)) * FAMILY_LIGHTNESS_SPREAD
        )
    new_l = max(FAMILY_LIGHTNESS_MIN, min(FAMILY_LIGHTNESS_MAX, new_l))
    return _rgb_to_hex(*colorsys.hls_to_rgb(h, new_l, s))


def assign_panel_colors(cohorts: list[str]) -> dict[str, str]:
    """Family ramp computed per panel, so a panel with one Anthropic cohort
    gets the pure brand color instead of an arbitrary point on the ramp."""
    members: dict[str, list[str]] = defaultdict(list)
    for cohort in cohorts:
        members[COHORT_MAP[cohort][1]].append(cohort)

    colors: dict[str, str] = {}
    for family, family_cohorts in members.items():
        base = MODEL_FAMILY_COLORS.get(family, "#666666")
        ramped = [c for c in family_cohorts if c not in COHORT_COLOR_OVERRIDES]
        for i, cohort in enumerate(ramped):
            colors[cohort] = vary_within_family(base, i, len(ramped))
        for cohort in family_cohorts:
            if cohort in COHORT_COLOR_OVERRIDES:
                colors[cohort] = COHORT_COLOR_OVERRIDES[cohort]
    return colors


# ============================================================================
# PLOT
# ============================================================================
def draw_line_panel(
    ax,
    agg: dict[str, dict[str, dict]],
    cohorts: list[str],
    difficulties: list[str],
    colors: dict[str, str],
    linestyles: dict[str, object],
    statistic: str,
    show_xticklabels: bool,
    err_style: str,
    smooth: bool,
    band_floor: float | None = None,
):
    """Draw one panel; returns (handles, labels) for a figure-level legend.

    band_floor clamps the lower band edge, which matters on the log time axis:
    mean - SEM can go non-positive, and matplotlib masks non-positive values on
    a log scale, which would punch a hole in the ribbon.
    """
    if not cohorts or not difficulties:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return [], []

    x_full = np.arange(len(difficulties))
    handles, labels = [], []

    for cohort in cohorts:
        color = colors[cohort]
        xs, centers, err_lo, err_hi = [], [], [], []
        for xi, difficulty in zip(x_full, difficulties):
            cell = agg.get(cohort, {}).get(difficulty)
            if not cell or cell["n"] == 0:
                continue
            center, lo, hi = center_and_err(cell, statistic)
            xs.append(xi)
            centers.append(center)
            err_lo.append(lo)
            err_hi.append(hi)
        if not xs:
            continue

        centers_arr = np.asarray(centers, dtype=float)
        lo_arr = centers_arr - np.asarray(err_lo, dtype=float)
        hi_arr = centers_arr + np.asarray(err_hi, dtype=float)
        if band_floor is not None:
            lo_arr = np.maximum(lo_arr, band_floor)

        if smooth and len(xs) >= 2:
            x_line, y_line = smooth_curve(xs, centers)
            x_band, y_lo = smooth_curve(xs, lo_arr)
            _, y_hi = smooth_curve(xs, hi_arr)
        else:
            x_line = np.asarray(xs, dtype=float)
            y_line, x_band, y_lo, y_hi = centers_arr, x_line, lo_arr, hi_arr

        if err_style == "band":
            ax.fill_between(
                x_band, y_lo, y_hi, color=color, alpha=BAND_ALPHA, linewidth=0, zorder=1
            )

        (line_artist,) = ax.plot(
            x_line,
            y_line,
            color=color,
            linewidth=LINE_WIDTH,
            alpha=LINE_ALPHA,
            linestyle=linestyles.get(cohort, "-"),
            zorder=2,
            solid_capstyle="round",
            solid_joinstyle="round",
            dash_capstyle="round",
        )

        if err_style == "bar":
            # Bars stay at the discrete knots whatever the smoothing does --
            # they report aggregated uncertainty, not interpolated values.
            ax.errorbar(
                xs,
                centers,
                yerr=[err_lo, err_hi],
                fmt="none",
                ecolor=color,
                elinewidth=ERR_LINEWIDTH,
                capsize=ERR_CAPSIZE,
                alpha=ERR_ALPHA,
                zorder=2,
            )

        # Markers sit on the real bucket values, so the smoothing stays
        # decorative rather than load-bearing.
        ax.plot(
            xs,
            centers,
            marker="o",
            markersize=MARKER_SIZE,
            markerfacecolor=color,
            markeredgecolor=MARKER_EDGE_COLOR,
            markeredgewidth=MARKER_EDGE_WIDTH,
            linestyle="none",
            zorder=3,
        )
        handles.append(line_artist)
        labels.append(COHORT_MAP[cohort][0])

    ax.set_xticks(x_full)
    if show_xticklabels:
        ax.set_xticklabels(
            [DIFFICULTY_LABELS.get(d, d) for d in difficulties],
            fontsize=DIFFICULTY_TICK_FS,
        )
    else:
        ax.set_xticklabels([])
    ax.tick_params(axis="x", length=0, pad=4)
    ax.tick_params(axis="y", labelsize=TICK_FONTSIZE)
    ax.set_xlim(-0.4, len(difficulties) - 0.6)

    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#888888")
        ax.spines[spine].set_linewidth(0.8)

    ax.yaxis.grid(True, which="major", linestyle="-", color="#E5E5E5", linewidth=0.9)
    ax.yaxis.grid(True, which="minor", linestyle=":", color="#EFEFEF", linewidth=0.6)
    ax.set_axisbelow(True)

    return handles, labels


def legend_dash_pattern() -> list[float]:
    """Dash spec for a two-stub legend swatch that ends where a solid one does.

    A solid swatch is a round-capped line drawn along the whole handle box, so
    it starts half a linewidth before the box and ends half a linewidth after
    it. Matching that means the second stub's dash segment has to finish exactly
    at the end of the handle box -- then its round cap overhangs by the same
    half linewidth, and the dashed entries line up with the solid ones on both
    sides.

    Dash specs are given in linewidth units, hence the division at the end;
    that same scaling is why the in-plot PIPELINE_LINESTYLES pattern can't be
    reused at LEGEND_HANDLE_LINEWIDTH, where one "on" segment would outrun the
    whole handle and the swatch would read as solid.
    """
    handle = LEGEND_HANDLELENGTH * LEGEND_FONTSIZE
    lw = LEGEND_HANDLE_LINEWIDTH
    # Round caps eat lw/2 off each side of the gap, so the dash spec's "off"
    # has to carry the visible gap plus a full linewidth. What is left of the
    # handle splits evenly between the two stubs.
    off = LEGEND_DASHED_GAP + lw
    on = (handle - off) / 2
    if on <= 0:
        raise ValueError(
            f"LEGEND_DASHED_GAP={LEGEND_DASHED_GAP} leaves no room for two stubs "
            f"in a {handle:.1f}pt handle"
        )
    return [on / lw, off / lw]


def pack_legend_lefts(centers, widths, band_left, band_right, gap):
    """Left edges for legend blocks that want to sit at `centers` but must fit.

    Each block would ideally be centred on its own column. When the blocks are
    wide that is unsatisfiable, so this projects the ideal placement onto the
    feasible set: inside [band_left, band_right], in column order, at least
    `gap` apart. The forward pass enforces the left edge and the gap against the
    previous block; the backward pass enforces the right edge and the gap
    against the next one. A block only moves as far as those constraints push
    it, so any slack stays where the ideal placement wanted it.

    Widths are figure fractions, as are the returned lefts. Returns
    (lefts, overflow) where overflow is how much width the band is short by --
    zero when the packing is clean.
    """
    lefts = [c - w / 2 for c, w in zip(centers, widths)]
    for i in range(len(lefts)):
        lefts[i] = max(lefts[i], band_left)
        if i:
            lefts[i] = max(lefts[i], lefts[i - 1] + widths[i - 1] + gap)
    for i in reversed(range(len(lefts))):
        lefts[i] = min(lefts[i], band_right - widths[i])
        if i + 1 < len(lefts):
            lefts[i] = min(lefts[i], lefts[i + 1] - gap - widths[i])
    # The backward pass can push the first block back past the band's left edge
    # when the blocks simply do not fit; report it rather than silently drawing
    # a legend that runs off the canvas.
    overflow = max(0.0, band_left - lefts[0])
    return [max(x, band_left) for x in lefts], overflow


def legend_frame(fig, legend):
    """Drawn bounding box of a legend, in figure fractions."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    return legend.get_window_extent(renderer).transformed(fig.transFigure.inverted())


def legend_size(fig, legend) -> tuple[float, float]:
    """Drawn (width, height) of a legend as figure fractions."""
    box = legend_frame(fig, legend)
    return box.width, box.height


def legend_row_centers(legend, renderer, tol=2.0) -> list[float]:
    """y centre of each row of entries, ascending, in display coordinates.

    Taken from the handle swatches. They are the one part of a legend that sits
    on the row's optical centre -- the label text is placed by its line box,
    which reserves ascent above the capitals that no glyph uses, so a box drawn
    around the labels sits higher than the rows appear to.

    A multi-column legend has several handles per row, so co-linear handles are
    clustered: they land on the same centre to within a fraction of a pixel.
    """
    centers = sorted(
        (h.get_window_extent(renderer).y0 + h.get_window_extent(renderer).y1) / 2
        for h in legend.get_lines()
    )
    rows = []
    for c in centers:
        if not rows or c - rows[-1] > tol:
            rows.append(c)
    return rows


class LegendDivider(Artist):
    """A rule drawn between two legend blocks, placed at draw time.

    Placement has to happen here rather than up front. A legend is anchored by a
    point in figure fractions, and savefig(bbox_inches="tight") re-renders into
    a canvas of a different size, at which point the legends re-resolve that
    anchor against the new canvas -- an artist whose coordinates were computed
    against the old one does not follow, and lands ~20px off in the saved file
    while looking perfectly aligned in memory. Measuring the legends with the
    renderer that is drawing them, in display coordinates, keeps the rule with
    them whatever canvas it ends up on.
    """

    zorder = 5

    def __init__(self, left, right, color, linewidth, overhang):
        super().__init__()
        self._left = left
        self._right = right
        self._color = color
        self._linewidth = linewidth
        self._overhang = overhang

    @staticmethod
    def _content_bounds(legend, renderer) -> tuple[float, float]:
        """x of the leftmost and rightmost thing a legend actually draws.

        The frame is not the right reference: it stands off the entries by
        borderpad, so the midpoint between two frames is not the midpoint
        between the label that ends one block and the swatch that starts the
        next -- which is the gap the eye measures.
        """
        boxes = [
            a.get_window_extent(renderer)
            for a in [*legend.get_texts(), *legend.get_lines()]
        ]
        return min(b.x0 for b in boxes), max(b.x1 for b in boxes)

    def draw(self, renderer):
        if not self.get_visible():
            return
        left_box = self._left.get_window_extent(renderer)
        rows = sorted(
            legend_row_centers(self._left, renderer)
            + legend_row_centers(self._right, renderer)
        )
        span = rows[-1] - rows[0]
        # Run the rule past the first and last rows by a share of the row pitch.
        # One row has no pitch to speak of, so fall back to the block's height.
        n = len(legend_row_centers(self._left, renderer))
        n = max(n, len(legend_row_centers(self._right, renderer)))
        pitch = span / (n - 1) if n > 1 else left_box.height
        half = span / 2 + self._overhang * pitch
        mid = (rows[0] + rows[-1]) / 2

        gap_start = self._content_bounds(self._left, renderer)[1]
        gap_end = self._content_bounds(self._right, renderer)[0]
        line = Line2D(
            [(gap_start + gap_end) / 2] * 2,
            [mid - half, mid + half],
            transform=IdentityTransform(),
            color=self._color,
            linewidth=self._linewidth,
            solid_capstyle="round",
        )
        line.set_figure(self.get_figure())
        line.draw(renderer)


def perf_y_bounds(agg, cohorts, difficulties, statistic) -> tuple[float, float]:
    uppers = [
        center_and_err(cell, statistic)[0] + center_and_err(cell, statistic)[2]
        for cohort in cohorts
        for cell in [agg.get(cohort, {}).get(d) for d in difficulties]
        if cell and cell["n"]
    ]
    if not uppers:
        return (0.0, 100.0)
    return (0.0, min(100.0, max(uppers) * 1.10))


def time_y_bounds(agg, cohorts, difficulties, statistic, log_scale):
    centers, uppers = [], []
    for cohort in cohorts:
        for d in difficulties:
            cell = agg.get(cohort, {}).get(d)
            if not cell or not cell["n"]:
                continue
            center, _, hi = center_and_err(cell, statistic)
            centers.append(center)
            uppers.append(center + hi)
    if not centers:
        return (0.1, 1.0) if log_scale else (0.0, 1.0)
    top = max(uppers) * (1.25 if log_scale else 1.10)
    if not log_scale:
        return (0.0, top)
    positive = [c for c in centers if c > 0]
    return ((min(positive) / 2.0) if positive else 0.1, top)


# ============================================================================
# MAIN
# ============================================================================
def main():
    global SAVE_DIR

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--results",
        default=None,
        help="path to a v1_results.json written by operation/v1/get_results.py "
        "(default: the newest run under operation/results/v1/)",
    )
    parser.add_argument(
        "--save-dir",
        default=SAVE_DIR,
        help=f"directory the figures are written to (default: {SAVE_DIR})",
    )
    parser.add_argument(
        "--metric",
        choices=METRIC_CHOICES,
        default="total",
        help="score column for the top row (default: total, the composite)",
    )
    parser.add_argument(
        "--statistic",
        choices=["mean", "median"],
        default="mean",
        help="center statistic for both rows; mean carries SEM, median carries "
        "the IQR (default: mean)",
    )
    parser.add_argument(
        "--include-all-buckets",
        action="store_true",
        help=f"also plot buckets excluded by default ({', '.join(EXCLUDED_BUCKETS)})",
    )
    y_group = parser.add_mutually_exclusive_group()
    y_group.add_argument(
        "--free-y",
        dest="free_y",
        action="store_true",
        help="give every panel its own y range, so each harness reads on its "
        "own terms (default: on)",
    )
    y_group.add_argument(
        "--shared-y",
        dest="free_y",
        action="store_false",
        help="one y range per row, shared across the columns; the panels stay "
        "comparable by eye at the cost of squashing whichever column occupies "
        "the smaller range",
    )
    parser.set_defaults(free_y=True)
    parser.add_argument(
        "--time-y-scale",
        choices=["log", "linear"],
        default="log",
        help="bottom-row y scale (default: log -- times span ~2 orders of magnitude)",
    )
    parser.add_argument(
        "--err-style",
        choices=["band", "bar"],
        default="band",
        help="uncertainty rendering: 'band' shades a ribbon around each line, "
        "'bar' draws vertical segments at each marker (default: band)",
    )
    smooth_group = parser.add_mutually_exclusive_group()
    smooth_group.add_argument(
        "--smooth",
        dest="smooth",
        action="store_true",
        help="smooth lines and bands with shape-preserving PCHIP interpolation "
        "(default: on)",
    )
    smooth_group.add_argument(
        "--no-smooth",
        dest="smooth",
        action="store_false",
        help="draw straight segments between buckets",
    )
    parser.set_defaults(smooth=True)
    args = parser.parse_args()
    SAVE_DIR = args.save_dir

    results_path = args.results or latest_results_path()
    print(f"[plot_pvt] reading {results_path}")

    score_rows, time_rows, pipelines = load_cells(results_path, args.metric)
    if not score_rows:
        raise SystemExit("No cohorts matched COHORT_MAP -- nothing to plot.")

    excluded = set() if args.include_all_buckets else set(EXCLUDED_BUCKETS)
    present = {d for pairs in score_rows.values() for d, _ in pairs}
    difficulties = [d for d in DIFFICULTY_ORDER if d in present and d not in excluded]
    dropped = [d for d in DIFFICULTY_ORDER if d in present and d in excluded]
    if dropped:
        print(f"[plot_pvt] excluded buckets: {', '.join(dropped)}")

    # Columns follow COLUMNS; inside a column, cohorts follow that column's
    # pipeline order and then COHORT_MAP order.
    by_column: dict[str, list[str]] = {}
    for key, _, column_pipelines in COLUMNS:
        by_column[key] = [
            cohort
            for pipeline in column_pipelines
            for cohort in COHORT_MAP
            if cohort in score_rows and pipelines[cohort] == pipeline
        ]
    columns = [(key, title) for key, title, _ in COLUMNS if by_column[key]]
    if not columns:
        raise SystemExit("No column has any cohort -- nothing to plot.")

    unplaced = sorted(
        {pipelines[c] for c in score_rows}
        - {p for _, _, pipes in COLUMNS for p in pipes}
    )
    if unplaced:
        print(
            f"[plot_pvt] WARNING: no column claims pipeline(s) {', '.join(unplaced)} "
            "-- those cohorts are not drawn. Add them to COLUMNS."
        )

    perf_agg = aggregate(score_rows, difficulties)
    time_agg = aggregate(time_rows, difficulties)

    print("[plot_pvt] per-cohort n (scored cells / timed attempts):")
    for key, title in columns:
        print(f"  [{title}]")
        for cohort in by_column[key]:
            n_score = sum(1 for d, _ in score_rows[cohort] if d in difficulties)
            n_time = sum(1 for d, _ in time_rows.get(cohort, []) if d in difficulties)
            print(
                f"    {COHORT_MAP[cohort][0]:<26} {pipelines[cohort]:<11} "
                f"scored={n_score:<4} timed={n_time:<4}"
                + ("  <- no timing" if n_time == 0 else "")
            )

    log_time = args.time_y_scale == "log"
    fig, axes = plt.subplots(
        2,
        len(columns),
        figsize=FIG_SIZE,
        sharex="col",
        sharey=False,
        squeeze=False,
    )

    # Panels get their own y range by default; --shared-y instead fits one range
    # per row across the columns. Rows never share a range with each other --
    # score and time are different scales -- so the shared case only ever ties
    # the columns of a single row together, which keeps the API/GUI gap readable
    # as an absolute gap rather than a per-panel one.
    all_cohorts = [c for key, _ in columns for c in by_column[key]]
    shared_perf = perf_y_bounds(perf_agg, all_cohorts, difficulties, args.statistic)
    shared_time = time_y_bounds(
        time_agg, all_cohorts, difficulties, args.statistic, log_time
    )

    dash_pattern = legend_dash_pattern()
    legend_entries: dict[str, tuple[list, list]] = {}
    for col, (key, title) in enumerate(columns):
        cohorts = by_column[key]
        colors = assign_panel_colors(cohorts)
        linestyles = {
            cohort: PIPELINE_LINESTYLES.get(pipelines[cohort], "-")
            for cohort in cohorts
        }
        ax_perf, ax_time = axes[0, col], axes[1, col]

        handles, labels = draw_line_panel(
            ax_perf,
            perf_agg,
            cohorts,
            difficulties,
            colors,
            linestyles,
            statistic=args.statistic,
            show_xticklabels=False,
            err_style=args.err_style,
            smooth=args.smooth,
        )
        legend_entries[key] = (handles, labels)
        ax_perf.set_ylim(
            *(
                perf_y_bounds(perf_agg, cohorts, difficulties, args.statistic)
                if args.free_y
                else shared_perf
            )
        )
        ax_perf.set_title(title, fontsize=PANEL_TITLE_FS, pad=10)

        bottom, top = (
            time_y_bounds(time_agg, cohorts, difficulties, args.statistic, log_time)
            if args.free_y
            else shared_time
        )
        # Set the scale and limits before drawing so the band floor is a real
        # axis coordinate rather than a guess.
        if log_time:
            ax_time.set_yscale("log")
        draw_line_panel(
            ax_time,
            time_agg,
            cohorts,
            difficulties,
            colors,
            linestyles,
            statistic=args.statistic,
            show_xticklabels=True,
            err_style=args.err_style,
            smooth=args.smooth,
            band_floor=bottom if log_time else 0.0,
        )
        ax_time.set_ylim(bottom, top)

        # With a shared row scale the repeated tick labels are noise.
        if col > 0 and not args.free_y:
            ax_perf.tick_params(axis="y", labelleft=False)
            ax_time.tick_params(axis="y", labelleft=False)

    stat_word = "Median" if args.statistic == "median" else "Mean"
    axes[0, 0].set_ylabel(
        f"{stat_word} {METRIC_DISPLAY[args.metric]}",
        fontsize=LABEL_FONTSIZE,
        labelpad=8,
    )
    axes[1, 0].set_ylabel(
        f"{stat_word} time (mins)", fontsize=LABEL_FONTSIZE, labelpad=8
    )

    # tight_layout sizes the panels normally; the hspace bump afterwards carves
    # the legend gap. tight_layout knows nothing about figure legends, so the
    # gap has to be opened after it runs.
    fig.tight_layout(rect=(0, 0.03, 1, 1), w_pad=2.0, h_pad=1.6)
    fig.subplots_adjust(hspace=LEGEND_GAP_FRAC)

    # Draw the legends first and measure them, then pack: their widths are only
    # known once they exist, and the packing needs every width up front.
    drawn_legends, want_centers = [], []
    for col, (key, _) in enumerate(columns):
        handles, labels = legend_entries[key]
        if not handles:
            continue
        # Measure the gap off the drawn axes so the legend follows the layout.
        top_box = axes[0, col].get_position()
        bottom_box = axes[1, col].get_position()
        band_y = (top_box.y0 + bottom_box.y1) / 2
        want_centers.append((top_box.x0 + top_box.x1) / 2)
        legend = fig.legend(
            handles,
            labels,
            # Anchored by its left edge so the packed x can be applied directly;
            # the provisional anchor here is overwritten below.
            loc="center left",
            bbox_to_anchor=(top_box.x0, band_y),
            # Without this fig.legend insets the frame from the anchor, and the
            # packed x would land the frame somewhere other than where it was
            # computed to go -- including past the band's right edge.
            borderaxespad=0.0,
            ncol=max(1, -(-len(handles) // LEGEND_MAX_ROWS)),
            fontsize=LEGEND_FONTSIZE,
            frameon=LEGEND_FRAMEON,
            facecolor=LEGEND_FACECOLOR,
            edgecolor=LEGEND_EDGECOLOR,
            framealpha=LEGEND_FRAMEALPHA,
            borderpad=LEGEND_BORDERPAD,
            labelspacing=LEGEND_LABELSPACING,
            handlelength=LEGEND_HANDLELENGTH,
            handletextpad=LEGEND_HANDLETEXTPAD,
            columnspacing=LEGEND_COLUMNSPACING,
        )
        # Thicken the swatches independently of the in-plot LINE_WIDTH; without
        # this the legend inherits the source artist's width. Legend lines come
        # back in handle order, so each one can be matched to the artist it was
        # copied from and re-dashed if that artist is dashed.
        for legend_line, source in zip(legend.get_lines(), handles):
            legend_line.set_linewidth(LEGEND_HANDLE_LINEWIDTH)
            if source.get_linestyle() != "-":
                legend_line.set_dashes(dash_pattern)
                legend_line.set_dash_capstyle("round")
        drawn_legends.append((legend, band_y))

    if drawn_legends:
        widths = [legend_size(fig, legend)[0] for legend, _ in drawn_legends]
        lefts, overflow = pack_legend_lefts(
            want_centers,
            widths,
            LEGEND_BAND_LEFT,
            axes[0, -1].get_position().x1 + LEGEND_BAND_RIGHT_OVERHANG,
            LEGEND_MIN_GAP,
        )
        if overflow:
            print(
                f"[plot_pvt] warning: legends overflow the figure by "
                f"{overflow:.3f} of its width -- lower LEGEND_FONTSIZE "
                f"({LEGEND_FONTSIZE}) or raise LEGEND_MAX_ROWS "
                f"({LEGEND_MAX_ROWS}, which trades width for height)"
            )
        for (legend, band_y), left in zip(drawn_legends, lefts):
            legend.set_bbox_to_anchor((left, band_y))

        # One rule per gap. It positions itself when drawn -- see LegendDivider.
        if LEGEND_DIVIDER:
            for (left_legend, _), (right_legend, _) in zip(
                drawn_legends, drawn_legends[1:]
            ):
                fig.add_artist(
                    LegendDivider(
                        left_legend,
                        right_legend,
                        LEGEND_DIVIDER_COLOR,
                        LEGEND_DIVIDER_LINEWIDTH,
                        LEGEND_DIVIDER_OVERHANG,
                    )
                )

    fig.supxlabel("Task difficulty (1 = easiest)", fontsize=LABEL_FONTSIZE, y=0.005)

    suffix = (
        f"_{args.metric}_{args.statistic}_time-{args.time_y_scale}"
        f"_err-{args.err_style}_{'smooth' if args.smooth else 'raw'}"
    )
    save_figure(fig, f"{SAVE_NAME}{suffix}")


def save_figure(fig, name):
    """Save a figure to SAVE_DIR as both PNG (at SAVE_DPI) and PDF."""
    os.makedirs(SAVE_DIR, exist_ok=True)
    out_png = os.path.join(SAVE_DIR, f"{name}.png")
    out_pdf = os.path.join(SAVE_DIR, f"{name}.pdf")
    fig.savefig(out_png, dpi=SAVE_DPI, bbox_inches="tight", pad_inches=0.15)
    fig.savefig(out_pdf, bbox_inches="tight", pad_inches=0.15)
    print(f"Saved: {out_png}")
    print(f"Saved: {out_pdf}")


if __name__ == "__main__":
    main()
