"""
Side-by-side figure (v1 -- data-driven):
  Left  -- Hierarchical evaluation rubric (typographic, with weight-bar percentages).
  Right -- Weighted-score stacked bar chart across cohorts, with per-pipeline hatching.

Same visual identity as BizbenchV1/judge/experiment_scripts/plot_main_v2.py, but the
per-cohort numbers come from the run manifest written by operation/v1/get_results.py
(operation/results/v1/<timestamp>/v1_results.json) instead of raw_grade_data.json.

Scoring matches get_results.py exactly: one row per (cohort x task); rows without a
clean grading (needs_grading / needs_attempt) score 0 on all four metrics, and the
denominator is max(rows, EXPECTED_TASKS). So the composite printed by get_results.py
is the height of the bar here.

Saves to ../../results/plots/main_and_rubric.{png,pdf} relative to this file
(i.e. operation/results/plots/). All adjustable items are at the top.

Usage:
    python operation/v1/paper_scripts/plot_main.py
    python operation/v1/paper_scripts/plot_main.py --results operation/results/v1/<ts>/v1_results.json
"""

import argparse
import json
import os

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.legend_handler import HandlerPatch
from matplotlib.path import Path
from matplotlib.transforms import ScaledTranslation

# ============================================================================
# OUTPUT
# ============================================================================
_HERE = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.normpath(os.path.join(_HERE, "../../results", "v1", "plots", "main"))
SAVE_NAME = "main_and_rubric"
SAVE_NAME_RUBRIC = "rubric"  # left panel, saved standalone
SAVE_NAME_BARS = "main_bars"  # right panel, saved standalone
SAVE_DPI = 200

# ============================================================================
# DATA SOURCE
# ============================================================================
# Run manifests live in operation/results/<version>/<timestamp>/. With no
# --results argument the newest timestamp directory is used (they sort
# lexicographically because the stamp is YYYYMMDDTHHMMSSZ).
RESULTS_ROOT = os.path.normpath(os.path.join(_HERE, "../../results", "v1"))
RESULTS_FILENAME = "v1_results.json"

# Floor on the mean's denominator -- must match VERSIONS["v1"]["expected_tasks"]
# in get_results.py. A cohort that came back with fewer cells is padded with
# zeros so it stays comparable to the rest.
EXPECTED_TASKS = 206

# Map get_results.py cohort label -> (display_name, family). The pipeline
# (api | gui | coding_cli) comes from the manifest, not from here. Cohorts
# present in this map and in the manifest are plotted; others are skipped.
# Order here is irrelevant -- bars are sorted by composite score below.
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

# Pipeline values from the manifest. Bars are ordered by composite score alone
# (ascending, so the chart climbs left->right); the harness is read off the
# hatching drawn over each bar. This list only fixes the legend's order.
GROUP_ORDER = ["api", "gui", "coding_cli"]
GROUP_LABELS = {"api": "API", "gui": "GUI", "coding_cli": "CODE"}

# ============================================================================
# GLOBAL STYLE
# ============================================================================
FONT_FAMILY = "DejaVu Sans"

# ----- ALL FONT SIZES (single source of truth) -----
# Right panel (bar chart)
TICK_FONTSIZE = 22
LABEL_FONTSIZE = 22
LEGEND_FONTSIZE = 22
BAR_VALUE_FS = 20
BAR_XLABEL_FS = 20
BAR_TITLE_FS = 20

# Left panel (rubric)
RUBRIC_TITLE_FS = 26
RUBRIC_PARENT_FS = 24
RUBRIC_PCT_PARENT_FS = 24
RUBRIC_CHILD_FS = 20
RUBRIC_PCT_FS = 20
RUBRIC_WBAR_X0 = 0.715
RUBRIC_WBAR_X1 = 0.85

#### FONT
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["font.family"] = "Helvetica"
plt.rcParams["mathtext.fontset"] = "stixsans"

#  ============================================================================
# FIGURE LAYOUT
# ============================================================================
FIG_SIZE = (22, 7.5)
WIDTH_RATIOS = [0.85, 2.15]  # [rubric, bars]
WSPACE = 0.12

# Standalone panel sizes (used when each plot is also saved on its own).
# The rubric is drawn entirely in axis-fraction coordinates tuned to the left
# panel's aspect ratio inside the combined figure (~5.6 x 5.3 in). Keep that
# same aspect here -- otherwise the fixed-position weight bars overlap the text.
FIG_SIZE_RUBRIC = (5.6, 5.3)
FIG_SIZE_BARS = (15.0, 7.5)

CATEGORY_COLORS = {
    "Accuracy": "#31588F",
    "Formula": "#C89257",
    "Format": "#34662F",
    "GROUP_HANDLE": "#888888",
}

MODEL_FAMILY_COLORS = {
    "AI2": "#0A3D62",
    "Alibaba": "#0040FF",
    "Moonshot": "#55B3C5",
    "OpenAI": "#000000",
    "xAI": "#1D9BF0",
    "Google": "#8B5CF6",
    "Anthropic": "#F25C1F",
}

MODEL_FAMILY_LABEL_TINT = 0.65
MODEL_FAMILY_LABEL_BASE = "#222222"

# ============================================================================
# PLOT 1 -- HIERARCHICAL RUBRIC
# ============================================================================
RUBRIC = [
    (
        "Accuracy",
        0.50,
        [
            ("Final Calculations", 0.35),
            ("Starting Values", 0.25),
            ("Task Completed", 0.25),
            ("Sign Consistency", 0.15),
        ],
    ),
    (
        "Formula",
        0.35,
        [
            ("Logic Readability & Size", 0.20),
            ("Handle Edge Cases", 0.20),
            ("Avoid Hardcoding", 0.20),
            ("Range Hygiene", 0.20),
            ("Using Absolute References", 0.20),
        ],
    ),
    (
        "Format",
        0.15,
        [
            ("Workbook & Sheet Structure", 0.20),
            ("Number Notation", 0.20),
            ("Readability", 0.10),
            ("Color Standards", 0.10),
            ("Information Alignment", 0.10),
            ("Style Consistency", 0.10),
            ("Borders & Shading", 0.10),
            ("Output Presentation", 0.10),
        ],
    ),
]

RUBRIC_TITLE = "Evaluation rubric"

RUBRIC_PARENT_LINE_H = 1.25
RUBRIC_CHILD_LINE_H = 1.05
RUBRIC_PARENT_GAP = 0.45

RUBRIC_PARENT_TEXT_X = 0.045
RUBRIC_CHILD_TEXT_X = 0.075
RUBRIC_WBAR_HEIGHT = 0.26
RUBRIC_PCT_X = 0.985

RUBRIC_BLOCK_BG_ALPHA = 0.06
RUBRIC_BLOCK_PAD_X = 0.005
RUBRIC_BLOCK_PAD_Y = 0.10
RUBRIC_BLOCK_ROUND = 0.04
RUBRIC_ACCENT_W = 0.014
RUBRIC_WBAR_BG_ALPHA = 0.16
RUBRIC_PILL_PAD = 0.012

# ============================================================================
# PLOT 2 -- STACKED WEIGHTED-SCORE BARS
# ============================================================================
# Component weights MUST match the weights the manifest was generated with
# (composite_formula in v1_results.json -- accuracy=0.50 formula=0.35 format=0.15).
# The loader checks them against the manifest and refuses to plot on a mismatch.
COMP_WEIGHTS = {"Accuracy": 0.50, "Formula": 0.35, "Format": 0.15}

# Standard-error bar styling on top of each composite bar.
SE_ECOLOR = "#222222"
SE_ELINEWIDTH = 1.4
SE_CAPSIZE = 4
SE_CAPTHICK = 1.4

# Harness (pipeline) encoding. The harness rides on the bar itself as a hatch
# pattern drawn in the hatch color over the component fills: API is single
# hatched, CODE cross-hatched, GUI left flat. The component color still carries
# accuracy/formula/format, so the hatch only has to separate three groups.
HARNESS_HATCH = {
    "api": "/",
    "gui": "",
    "coding_cli": "x",
}
HATCH_COLOR = "white"
HATCH_LINEWIDTH = 2.2
# Legend swatch for the harness handles: a neutral fill carrying the same white
# hatch the bars do, so the legend uses the bars' grammar (light-on-dark) rather
# than inverting it. Flat GUI then reads as "no pattern", not as an empty box.
HARNESS_LEGEND_FACE = "#8D97A3"
plt.rcParams["hatch.linewidth"] = HATCH_LINEWIDTH
XTICK_PAD = 6  # points

BAR_TITLE = ""
BAR_WIDTH = 0.72
BAR_YLABEL = "Composite Score"
BAR_YLIM = (0, 100)
BAR_VALUE_OFFSET = 0.9
# Shallow tilt: the label band's height is ~sin(rot) x label length, so a flatter
# angle buys back vertical space at almost no cost in horizontal footprint
# (cos changes slowly near 0). The trade is tighter spacing between the parallel
# label lines, which is why the font can't go much past BAR_XLABEL_FS here.
BAR_XLABEL_ROT = 22  # degrees
BAR_XLABEL_X_SHIFT_PT = 10  # rightward nudge (points) to compensate for label tilt
# Gap (in bar-slot units) between the y-axis and the left edge of the first bar,
# replacing matplotlib's default 5% x-margin on that side only.
BAR_XPAD_LEFT = 0.22

# Only the top of each bar is rounded (the two upper corners of the topmost
# non-empty segment); the stack's internal seams and its foot stay square.
BAR_TOP_ROUND_PT = 4  # corner radius, points

# Legend swatches echo that rounding, and run wider so the harness hatch reads
# as a pattern rather than one stray stroke.
LEGEND_HANDLE_ROUND_PT = 4  # corner radius, points
LEGEND_HANDLE_LENGTH = 3.0  # in font-size units, matplotlib's handlelength
# Upper-left anchor in axes fractions -- nudged in from the spine so the first
# swatch doesn't sit flush against the y-axis.
LEGEND_ANCHOR = (0.035, 1.0)


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


def load_models_from_results(path: str):
    """Build the MODELS list (name, acc_seg, formula_seg, format_seg, group) plus
    a per-cohort standard error from the per-(cohort, task) cells in v1_results.json.

    Cells without a clean grading score 0 on every metric, and a cohort with fewer
    than EXPECTED_TASKS cells is padded with zeros -- the same imputation
    get_results.py applies, so bar heights equal its reported mean_total.

    Component segments are mean component score * weight so they stack to the mean
    composite total. SE is the standard error of the per-task composite
    (sd / sqrt(n), ddof=1; 0.0 when n<2).

    Returns (models, families, ses, ns) -- parallel lists.
    """
    with open(path) as f:
        data = json.load(f)

    expected_formula = " + ".join(
        f"{COMP_WEIGHTS[k.capitalize()]:g}*{k}"
        for k in ("accuracy", "formula", "format")
    )
    if data.get("composite_formula") != expected_formula:
        raise SystemExit(
            f"COMP_WEIGHTS disagree with the manifest: plot expects "
            f"{expected_formula!r}, {path} was built with "
            f"{data.get('composite_formula')!r}"
        )

    cells_all = data.get("cells", [])
    if not cells_all:
        raise SystemExit(f"No cells in {path}")

    by_cohort: dict[str, list[dict]] = {}
    for c in cells_all:
        by_cohort.setdefault(c["cohort"], []).append(c)

    # (total, name, acc_seg, formula_seg, format_seg, group, family, se, n)
    rows = []
    for cohort, (display, family) in COHORT_MAP.items():
        bucket = by_cohort.get(cohort)
        if not bucket:
            print(f"[plot_main] skipping {cohort!r}: not in {os.path.basename(path)}")
            continue
        group = bucket[0]["pipeline"]

        def column(key: str) -> np.ndarray:
            """Metric column with ungraded cells imputed as 0."""
            return np.array(
                [(c[key] if c["status"] == "complete" else 0.0) for c in bucket],
                dtype=float,
            )

        accs = column("accuracy")
        formulas = column("formula")
        formats = column("format")
        totals = column("total")
        n = len(totals)

        if EXPECTED_TASKS and n < EXPECTED_TASKS:
            pad = EXPECTED_TASKS - n
            print(
                f"[plot_main] padding {cohort!r} with {pad} zero rows "
                f"(n={n} -> {EXPECTED_TASKS})"
            )
            zeros = np.zeros(pad, dtype=float)
            accs = np.concatenate([accs, zeros])
            formulas = np.concatenate([formulas, zeros])
            formats = np.concatenate([formats, zeros])
            totals = np.concatenate([totals, zeros])
            n = EXPECTED_TASKS

        acc_seg = float(accs.mean()) * COMP_WEIGHTS["Accuracy"]
        formula_seg = float(formulas.mean()) * COMP_WEIGHTS["Formula"]
        format_seg = float(formats.mean()) * COMP_WEIGHTS["Format"]
        total_mean = float(totals.mean())
        se_total = float(np.std(totals, ddof=1) / np.sqrt(n)) if n > 1 else 0.0

        rows.append(
            (
                total_mean,
                display,
                acc_seg,
                formula_seg,
                format_seg,
                group,
                family,
                se_total,
                n,
            )
        )

    # Sort by composite score alone, ascending, so the chart climbs left->right.
    rows.sort(key=lambda r: r[0])

    models = [(r[1], r[2], r[3], r[4], r[5]) for r in rows]
    families = [r[6] for r in rows]
    ses = [r[7] for r in rows]
    ns = [r[8] for r in rows]
    return models, families, ses, ns


# ============================================================================
# HELPERS
# ============================================================================
def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def _blend(c1, c2, t):
    """Linear blend between two hex colors. t=1 -> c1, t=0 -> c2."""
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return (
        r1 * t + r2 * (1 - t),
        g1 * t + g2 * (1 - t),
        b1 * t + b2 * (1 - t),
    )


_BEZIER_K = 0.5522847498  # circle-to-cubic-Bezier constant


def _rounded_top_path(x0, y0, w, h, rx, ry):
    """Rectangle (x0, y0, w, h) whose two TOP corners are rounded.

    rx/ry are the corner radii in data units -- they differ per axis because a
    round corner on screen is an ellipse in data space.
    """
    rx = min(rx, w / 2.0)
    ry = min(ry, h)
    kx, ky = _BEZIER_K * rx, _BEZIER_K * ry
    x1, y1 = x0 + w, y0 + h
    verts = [
        (x0, y0),
        (x0, y1 - ry),
        (x0, y1 - ry + ky),
        (x0 + rx - kx, y1),
        (x0 + rx, y1),
        (x1 - rx, y1),
        (x1 - rx + kx, y1),
        (x1, y1 - ry + ky),
        (x1, y1 - ry),
        (x1, y0),
        (x0, y0),
    ]
    codes = [
        Path.MOVETO,
        Path.LINETO,
        Path.CURVE4,
        Path.CURVE4,
        Path.CURVE4,
        Path.LINETO,
        Path.CURVE4,
        Path.CURVE4,
        Path.CURVE4,
        Path.LINETO,
        Path.CLOSEPOLY,
    ]
    return Path(verts, codes)


def _round_bar_tops(ax, seg_bars, order):
    """Swap the topmost non-empty segment of each bar for a rounded-top path.

    Must run after set_ylim -- the pixel radius is converted through transData,
    which only settles once the axes limits and position are final.
    """
    inv = ax.transData.inverted()
    r_px = BAR_TOP_ROUND_PT * ax.figure.dpi / 72.0
    (dx0, dy0), (dx1, dy1) = inv.transform([(0.0, 0.0), (r_px, r_px)])
    rx, ry = abs(dx1 - dx0), abs(dy1 - dy0)

    n_bars = len(seg_bars[order[0]][0])
    for i in range(n_bars):
        for key in order:
            bars, values = seg_bars[key]
            if values[i] <= 0:
                continue
            rect = bars[i]
            ax.add_patch(
                mpatches.PathPatch(
                    _rounded_top_path(
                        rect.get_x(),
                        rect.get_y(),
                        rect.get_width(),
                        rect.get_height(),
                        rx,
                        ry,
                    ),
                    facecolor=rect.get_facecolor(),
                    edgecolor=rect.get_edgecolor(),
                    hatch=rect.get_hatch(),
                    linewidth=0,
                    zorder=rect.get_zorder(),
                )
            )
            rect.remove()
            break


class _RoundedHandler(HandlerPatch):
    """Legend swatch drawn as a rounded rectangle instead of a hard-cornered one."""

    def create_artists(
        self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans
    ):
        # The handle box scales points -> pixels itself, so the radius is passed
        # through in points and stays 4pt at any save dpi.
        r = LEGEND_HANDLE_ROUND_PT
        patch = mpatches.FancyBboxPatch(
            (-xdescent, -ydescent),
            width,
            height,
            boxstyle=f"round,pad=0,rounding_size={r}",
            facecolor=orig_handle.get_facecolor(),
            edgecolor=orig_handle.get_edgecolor(),
            hatch=orig_handle.get_hatch(),
            linewidth=0,
        )
        patch.set_transform(trans)
        return [patch]


# ============================================================================
# PLOT 1: TYPOGRAPHIC RUBRIC
# ============================================================================
def draw_rubric(ax, rubric):
    ax.set_xlim(0, 1)
    ax.axis("off")

    total_h = 0.0
    for i, (_, _, children) in enumerate(rubric):
        total_h += RUBRIC_PARENT_LINE_H
        total_h += len(children) * RUBRIC_CHILD_LINE_H
        if i < len(rubric) - 1:
            total_h += RUBRIC_PARENT_GAP
    ax.set_ylim(0, total_h)

    y = total_h

    for i, (parent, parent_w, children) in enumerate(rubric):
        color = CATEGORY_COLORS.get(parent, "#888888")
        rgb = _hex_to_rgb(color)

        block_height = RUBRIC_PARENT_LINE_H + len(children) * RUBRIC_CHILD_LINE_H
        block_top = y
        block_bottom = y - block_height

        ax.add_patch(
            mpatches.FancyBboxPatch(
                (RUBRIC_BLOCK_PAD_X, block_bottom + RUBRIC_BLOCK_PAD_Y),
                1.0 - 2 * RUBRIC_BLOCK_PAD_X,
                block_height - 2 * RUBRIC_BLOCK_PAD_Y,
                boxstyle=f"round,pad=0,rounding_size={RUBRIC_BLOCK_ROUND}",
                facecolor=(*rgb, RUBRIC_BLOCK_BG_ALPHA),
                edgecolor="none",
                mutation_aspect=0.3,
            )
        )
        ax.add_patch(
            mpatches.FancyBboxPatch(
                (RUBRIC_BLOCK_PAD_X, block_bottom + RUBRIC_BLOCK_PAD_Y),
                RUBRIC_ACCENT_W,
                block_height - 2 * RUBRIC_BLOCK_PAD_Y,
                boxstyle=f"round,pad=0,rounding_size={RUBRIC_BLOCK_ROUND}",
                facecolor=color,
                edgecolor="none",
                mutation_aspect=0.3,
            )
        )

        y_parent_top = y
        y_parent_bottom = y - RUBRIC_PARENT_LINE_H
        y_parent_mid = (y_parent_top + y_parent_bottom) / 2

        ax.text(
            RUBRIC_PARENT_TEXT_X,
            y_parent_mid,
            parent,
            ha="left",
            va="center",
            fontsize=RUBRIC_PARENT_FS,
            fontweight="bold",
            color=color,
        )

        ax.text(
            RUBRIC_PCT_X,
            y_parent_mid,
            f"{int(parent_w * 100)}%",
            ha="right",
            va="center",
            fontsize=RUBRIC_PCT_PARENT_FS,
            fontweight="bold",
            color=color,
        )

        ax.plot(
            [RUBRIC_PARENT_TEXT_X, RUBRIC_PCT_X],
            [y_parent_bottom + 0.12, y_parent_bottom + 0.12],
            color=color,
            alpha=0.30,
            linewidth=0.9,
            solid_capstyle="butt",
        )

        y = y_parent_bottom

        wbar_full = RUBRIC_WBAR_X1 - RUBRIC_WBAR_X0
        max_child_w = max(cw for _, cw in children) if children else 1.0
        for child_name, child_w in children:
            y_child_top = y
            y_child_bottom = y - RUBRIC_CHILD_LINE_H
            y_child_mid = (y_child_top + y_child_bottom) / 2

            ax.text(
                RUBRIC_CHILD_TEXT_X,
                y_child_mid,
                child_name,
                ha="left",
                va="center",
                fontsize=RUBRIC_CHILD_FS,
                color="#222222",
            )

            ax.add_patch(
                mpatches.FancyBboxPatch(
                    (RUBRIC_WBAR_X0, y_child_mid - RUBRIC_WBAR_HEIGHT / 2),
                    wbar_full,
                    RUBRIC_WBAR_HEIGHT,
                    boxstyle="round,pad=0,rounding_size=0.10",
                    facecolor=(*rgb, RUBRIC_WBAR_BG_ALPHA),
                    edgecolor="none",
                    mutation_aspect=0.3,
                )
            )
            fill_w = wbar_full * (child_w / max_child_w)
            if fill_w > 0:
                ax.add_patch(
                    mpatches.FancyBboxPatch(
                        (RUBRIC_WBAR_X0, y_child_mid - RUBRIC_WBAR_HEIGHT / 2),
                        fill_w,
                        RUBRIC_WBAR_HEIGHT,
                        boxstyle="round,pad=0,rounding_size=0.10",
                        facecolor=color,
                        edgecolor="none",
                        alpha=0.92,
                        mutation_aspect=0.3,
                    )
                )

            ax.text(
                RUBRIC_PCT_X,
                y_child_mid,
                f"{int(child_w * 100)}%",
                ha="right",
                va="center",
                fontsize=RUBRIC_PCT_FS,
                fontweight="bold",
                color="#444444",
            )

            y = y_child_bottom

        if i < len(rubric) - 1:
            y -= RUBRIC_PARENT_GAP


# ============================================================================
# PLOT 2: STACKED WEIGHTED-SCORE BARS
# ============================================================================
def draw_bars(ax, models, families, ses):
    names = [m[0] for m in models]
    accs = np.array([m[1] for m in models])
    formulas = np.array([m[2] for m in models])
    formats = np.array([m[3] for m in models])
    groups = [m[4] for m in models]
    totals = accs + formulas + formats
    ses = np.array(ses, dtype=float)

    x = np.arange(len(models))
    hatches = [HARNESS_HATCH.get(g, "") for g in groups]

    # Hatch is drawn per bar (matplotlib takes one hatch per container, not per
    # patch), so the segments go up in three passes and each patch is stamped
    # with its cohort's harness pattern afterwards.
    seg_bars = {}
    for values, bottoms, key in (
        (accs, np.zeros_like(accs), "Accuracy"),
        (formulas, accs, "Formula"),
        (formats, accs + formulas, "Format"),
    ):
        bars = ax.bar(
            x,
            values,
            width=BAR_WIDTH,
            bottom=bottoms,
            color=CATEGORY_COLORS[key],
            edgecolor=HATCH_COLOR,
            linewidth=0,
        )
        for patch, hatch in zip(bars, hatches):
            if hatch:
                patch.set_hatch(hatch)
        seg_bars[key] = (bars, values)

    ax.errorbar(
        x,
        totals,
        yerr=ses,
        fmt="none",
        ecolor=SE_ECOLOR,
        elinewidth=SE_ELINEWIDTH,
        capsize=SE_CAPSIZE,
        capthick=SE_CAPTHICK,
        zorder=5,
    )

    for i, (t, se) in enumerate(zip(totals, ses)):
        ax.text(
            x[i],
            t + se + BAR_VALUE_OFFSET,
            f"{t:.1f}",
            ha="center",
            va="bottom",
            fontsize=BAR_VALUE_FS,
            fontweight="bold",
        )

    ax.set_xticks(x)
    ax.set_xticklabels(
        names, rotation=BAR_XLABEL_ROT, ha="right", fontsize=BAR_XLABEL_FS
    )
    ax.tick_params(axis="x", length=0, pad=XTICK_PAD)

    shift = ScaledTranslation(
        BAR_XLABEL_X_SHIFT_PT / 72.0, 0, ax.figure.dpi_scale_trans
    )
    for i, tick_label in enumerate(ax.get_xticklabels()):
        tick_label.set_transform(tick_label.get_transform() + shift)
        family = families[i] if i < len(families) else None
        brand = MODEL_FAMILY_COLORS.get(family)
        if brand is not None:
            tick_label.set_color(
                _blend(brand, MODEL_FAMILY_LABEL_BASE, MODEL_FAMILY_LABEL_TINT)
            )
            tick_label.set_fontweight("semibold")

    ax.set_ylabel(BAR_YLABEL, fontsize=LABEL_FONTSIZE)
    ax.set_ylim(*BAR_YLIM)
    # Tighten only the left margin; the right keeps its default breathing room.
    ax.set_xlim(x[0] - BAR_WIDTH / 2 - BAR_XPAD_LEFT, ax.get_xlim()[1])
    if BAR_TITLE:
        ax.set_title(BAR_TITLE, fontsize=BAR_TITLE_FS)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="y", labelsize=TICK_FONTSIZE)
    ax.yaxis.grid(True, linestyle=":", alpha=0.4)
    ax.set_axisbelow(True)

    # After the limits are final: soften the crown of each stack.
    _round_bar_tops(ax, seg_bars, ["Format", "Formula", "Accuracy"])

    comp_handles = [
        mpatches.Patch(color=CATEGORY_COLORS["Accuracy"], label="Accuracy"),
        mpatches.Patch(color=CATEGORY_COLORS["Formula"], label="Formula"),
        mpatches.Patch(color=CATEGORY_COLORS["Format"], label="Format"),
    ]
    # Only show harness handles for pipelines that actually have bars. They use
    # the neutral swatch so the pattern -- not a fourth color -- is what the eye
    # picks up next to the component handles.
    group_handles = [
        mpatches.Patch(
            facecolor=HARNESS_LEGEND_FACE,
            edgecolor=HATCH_COLOR,
            linewidth=0,
            hatch=HARNESS_HATCH.get(g, ""),
            label=GROUP_LABELS.get(g, g),
        )
        for g in GROUP_ORDER
        if g in set(groups)
    ]
    ax.legend(
        handles=comp_handles + group_handles,
        loc="upper left",
        bbox_to_anchor=LEGEND_ANCHOR,
        bbox_transform=ax.transAxes,
        ncol=2,
        frameon=False,
        fontsize=LEGEND_FONTSIZE,
        columnspacing=2.2,
        handlelength=LEGEND_HANDLE_LENGTH,
        handleheight=1.2,
        handletextpad=0.7,
        labelspacing=0.55,
        borderpad=0.0,
        handler_map={mpatches.Patch: _RoundedHandler()},
    )


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
    args = parser.parse_args()
    SAVE_DIR = args.save_dir

    results_path = args.results or latest_results_path()
    print(f"[plot_main] reading {results_path}")

    models, families, ses, ns = load_models_from_results(results_path)
    if not models:
        raise SystemExit("No cohorts matched COHORT_MAP -- nothing to plot.")
    print("[plot_main] per-cohort n (used for SE):")
    for (name, _, _, _, group), n, se in zip(models, ns, ses):
        print(f"  {name:<26} {group:<11} n={n:<4} SE(total)={se:.2f}")

    fig, (ax_left, ax_right) = plt.subplots(
        1,
        2,
        figsize=FIG_SIZE,
        gridspec_kw={"width_ratios": WIDTH_RATIOS, "wspace": WSPACE},
    )

    fig.subplots_adjust(left=0.04, right=0.99, top=0.94, bottom=0.18, wspace=WSPACE)

    draw_rubric(ax_left, RUBRIC)
    draw_bars(ax_right, models, families, ses)

    fig.canvas.draw()
    bbox_right = ax_right.get_position()
    bbox_left = ax_left.get_position()
    renderer = fig.canvas.get_renderer()
    tight_right = ax_right.get_tightbbox(renderer).transformed(
        fig.transFigure.inverted()
    )
    new_bottom = tight_right.y0
    new_top = bbox_right.y1
    ax_left.set_position(
        [bbox_left.x0, new_bottom, bbox_left.width, new_top - new_bottom]
    )

    save_figure(fig, SAVE_NAME)

    # --- Standalone panels ---------------------------------------------------
    # Re-draw each panel onto its own figure so the rubric and bar chart can be
    # used independently. Each is saved as both PNG and PDF.
    fig_rubric, ax_rubric = plt.subplots(figsize=FIG_SIZE_RUBRIC)
    # Let the axes fill the figure so the rubric keeps the same proportions it
    # has in the combined panel (no margins to reflow the fraction-positioned bars).
    fig_rubric.subplots_adjust(left=0.0, right=1.0, top=1.0, bottom=0.0)
    draw_rubric(ax_rubric, RUBRIC)
    save_figure(fig_rubric, SAVE_NAME_RUBRIC)

    fig_bars, ax_bars = plt.subplots(figsize=FIG_SIZE_BARS)
    draw_bars(ax_bars, models, families, ses)
    save_figure(fig_bars, SAVE_NAME_BARS)


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
