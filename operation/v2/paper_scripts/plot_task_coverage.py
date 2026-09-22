#!/usr/bin/env python
"""Plot task-type coverage of the v2 task set against prior spreadsheet benchmarks.

Reads the live `tasks` table of the v2 Neon database (config database.v2_url)
over the non-deprecated tasks and produces task_coverage.{png,pdf} under
operation/results/v2/plots/task_coverage/:

    Left   -- one row per task type in the expert taxonomy (TAXONOMY below,
              grouped into families on the far left), with our task count as a
              bar stacked by tasks.human_difficulty_measure. Types we have no
              task for read "not covered".
    Right  -- a dot matrix of the same rows against prior benchmarks: a filled
              dot with a count where the benchmark has tasks of that type, an
              empty dot where it has none.

Our counts come from case_classification.model_type, whose values are the
taxonomy's type names verbatim. Prior-benchmark counts live in PRIOR; those
named in PRIOR_PLACEHOLDER are still guesses, and the figure says so while any
remain.

Colours, typeface, and the bar/legend helpers are shared with
plot_difficulty_and_types.py in this directory. Run with the shared plotting
environment, ~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_task_coverage.py
    python operation/v2/paper_scripts/plot_task_coverage.py --plot-dir /tmp/plots
    python operation/v2/paper_scripts/plot_task_coverage.py --database-url postgresql://...
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    DIFFICULTY_COLORS,
    DIFFICULTY_ORDER,
    GRID,
    INK,
    REPO_ROOT,
    STYLE,
    SURFACE,
    UNKNOWN_COLOR,
    _RoundedHandler,
    database_url,
    squircle_bar_ends,
)

SLATE = STYLE["ink"]["slate"]  # secondary text: counts, notes, "not covered"
SLATE_LIGHT = STYLE["ink"]["slate_light"]  # empty dots

PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "task_coverage"
PLOT_NAME = "task_coverage"

OUR_NAME = "EnterpriseBench"

# Expert taxonomy: (family, case_classification.model_type, short row label).
# Row order is the figure's top-to-bottom order; families must be contiguous.
TAXONOMY = [
    ("Valuation", "DCF (Discounted Cash Flow)", "DCF"),
    ("Valuation", "Comparables (Trading Comps)", "Trading Comps"),
    ("Valuation", "Sum-of-the-Parts Valuation", "Sum-of-the-Parts"),
    ("Valuation", "NAV (Net Asset Value) Modeling", "NAV"),
    ("Valuation", "Precedent Transaction Analysis", "Precedent Transactions"),
    ("Fin. Statements", "3-Statement Model", "3-Statement"),
    ("Fin. Statements", "Working Capital Modeling", "Working Capital"),
    ("Fin. Statements", "Ratio Analysis", "Ratio Analysis"),
    ("FP&A", "Budget vs. Actuals Variance Analysis", "Budget vs. Actuals"),
    ("FP&A", "Forecasting Model", "Forecasting"),
    ("FP&A", "Scenario / Sensitivity Analysis", "Scenario / Sensitivity"),
    (
        "Debt & Financing",
        "Debt Schedule / Waterfall Modeling",
        "Debt Schedule / Waterfall",
    ),
    ("Debt & Financing", "Loan Amortization Schedules", "Loan Amortization"),
    (
        "Fixed Inc. & Deriv.",
        "Bond Pricing / Yield Curve Analysis",
        "Bond Pricing / Yield Curve",
    ),
    ("Personal Finance", "Rent vs. Buy Analysis", "Rent vs. Buy"),
    ("Personal Finance", "Retirement / Savings Calculators", "Retirement / Savings"),
    ("Business Analytics", "Non-Financial or Non-FP&A Model", "Non-financial model"),
    ("Deal / Transaction", "LBO (Leveraged Buyout)", "LBO"),
    ("Deal / Transaction", "Merger Model", "Merger Model"),
    ("Deal / Transaction", "Accretion/Dilution Analysis", "Accretion / Dilution"),
    ("Other", None, "Non-spreadsheet tasks"),  # prior benchmarks only
]

# Tasks per type (short row label) per prior benchmark; 0 / absent = not covered.
# BankerToolBench and GDPval are expert-labelled per task. Benchmarks listed in
# PRIOR_PLACEHOLDER still carry guessed counts and are flagged on the figure.
PRIOR_PLACEHOLDER = {"MBABench"}
PRIOR = {
    "BankerToolBench": {
        "DCF": 18,
        "Trading Comps": 10,
        "Precedent Transactions": 1,
        "LBO": 15,
        "Merger Model": 6,
        "Non-spreadsheet tasks": 50,
    },
    "GDPval (finance)": {
        "3-Statement": 1,
        "Scenario / Sensitivity": 1,
        "Rent vs. Buy": 1,
        "Retirement / Savings": 1,
        "Non-spreadsheet tasks": 16,
    },
    "MBABench": {
        "DCF": 4,
        "Trading Comps": 2,
        "3-Statement": 6,
        "Working Capital": 2,
        "Ratio Analysis": 3,
        "Forecasting": 5,
        "Scenario / Sensitivity": 3,
        "Budget vs. Actuals": 4,
        "Loan Amortization": 3,
        "Bond Pricing / Yield Curve": 2,
        "Retirement / Savings": 2,
        "Non-financial model": 2,
    },
}
PRIOR_ORDER = ["BankerToolBench", "GDPval (finance)", "MBABench"]
PRIOR_SHORT = {
    "BankerToolBench": "Banker\nToolBench",
    "GDPval (finance)": "GDPval\n(finance)",
    "MBABench": "MBABench\n(public)",
}

# ============================================================================
# FIGURE STYLE
# ============================================================================
FIG_SIZE = (16, 11)
WIDTH_RATIOS = [3.2, 1.4]  # [stacked bars, dot matrix]
WSPACE = 0.04
LEFT_MARGIN = 0.30  # figure fraction reserved for family brackets + row labels
SAVE_DPI = 200
SUBTITLE_FS = 14
TICK_FS = 15
VALUE_FS = 15
LEGEND_FS = 18
FAMILY_FS = 17
FAMILY_COLOR = DIFFICULTY_COLORS[
    "Hard"
]  # family labels and brackets: the ramp's navy, not grey
FAMILY_BRACKET_LW = 4  # thick navy bar so each family block reads as a header
FAMILY_LABEL_PAD = 0.018
BAR_HEIGHT = 0.66
DOT_FILL = DIFFICULTY_COLORS["Medium-Hard"]
DOT_SIZE_FILLED = 150
DOT_SIZE_EMPTY = 110
FAMILY_BRACKET_X = -0.32  # axes fraction; brackets sit left of the row labels

plt.rcParams["axes.unicode_minus"] = False

SQL = """
    SELECT human_difficulty_measure, case_classification->>'model_type'
    FROM tasks
    WHERE deprecated IS NOT TRUE
"""


def fetch_counts(conn) -> Counter:
    """Counter of (model_type, difficulty) over the non-deprecated tasks."""
    with conn.cursor() as cur:
        cur.execute(SQL)
        return Counter(
            (model_type, difficulty) for difficulty, model_type in cur.fetchall()
        )


def check_taxonomy(counts: Counter) -> None:
    """Every model_type in the database must be a taxonomy type, or the row is lost."""
    known = {t for _, t, _ in TAXONOMY}
    stray = sorted({mt for mt, _ in counts if mt not in known}, key=str)
    if stray:
        sys.exit(f"model_type values missing from TAXONOMY: {stray}")
    shorts = {s for _, _, s in TAXONOMY}
    for name, cov in PRIOR.items():
        bad = sorted(set(cov) - shorts)
        if bad:
            sys.exit(f"PRIOR[{name!r}] uses row labels not in TAXONOMY: {bad}")


def family_spans(y: list[float]) -> list[tuple[str, float, float, bool]]:
    """(family, y_top, y_bottom, has_next) for each contiguous family block."""
    spans, i, n = [], 0, len(TAXONOMY)
    while i < n:
        j = i
        while j + 1 < n and TAXONOMY[j + 1][0] == TAXONOMY[i][0]:
            j += 1
        spans.append((TAXONOMY[i][0], y[i] + 0.5, y[j] - 0.5, j + 1 < n))
        i = j + 1
    return spans


def plot_ours(ax, counts: Counter, y: list[float]) -> None:
    """Left panel: our task count per taxonomy row, stacked by difficulty."""
    left = [0] * len(TAXONOMY)
    outer = [None] * len(TAXONOMY)
    handles = []
    for d in reversed(
        DIFFICULTY_ORDER
    ):  # hardest segment at the base, easiest at the tip
        widths = [counts[(t, d)] if t else 0 for _, t, _ in TAXONOMY]
        bars = ax.barh(
            y,
            widths,
            left=left,
            height=BAR_HEIGHT,
            label=d,
            color=DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR),
            edgecolor=SURFACE,
            linewidth=1.0,
            zorder=2,
        )
        handles.append(bars)
        for i, (rect, w) in enumerate(zip(bars, widths)):
            if w > 0:
                outer[i] = rect
        left = [a + b for a, b in zip(left, widths)]

    biggest = max(left)
    for yi, total, (fam, _, _) in zip(y, left, TAXONOMY):
        if total > 0:
            ax.text(
                total + biggest * 0.02,
                yi,
                str(total),
                ha="left",
                va="center",
                fontsize=VALUE_FS,
                color=INK,
            )
        elif fam != "Other":
            ax.text(
                biggest * 0.02,
                yi,
                "not covered",
                ha="left",
                va="center",
                fontsize=VALUE_FS - 1,
                color=SLATE,
                style="italic",
            )
        else:
            ax.text(
                biggest * 0.02,
                yi,
                "–",
                ha="left",
                va="center",
                fontsize=VALUE_FS,
                color=SLATE,
            )

    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=SLATE, labelsize=TICK_FS, length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)
    ax.set_yticks(y, [short for _, _, short in TAXONOMY], fontsize=TICK_FS, color=INK)
    ax.set_xlim(0, biggest * 1.15)
    ax.set_ylim(y[-1] - 0.6, y[0] + 0.6)
    ax.legend(
        handles=[h[0] for h in reversed(handles)],
        labels=list(DIFFICULTY_ORDER),
        title="Difficulty",
        loc="lower right",
        fontsize=LEGEND_FS,
        title_fontsize=LEGEND_FS,
        frameon=False,
        labelcolor=INK,
        handler_map={mpatches.Rectangle: _RoundedHandler()},
    )

    # Family brackets and labels, in axes-x / data-y coordinates left of the row labels.
    trans = ax.get_yaxis_transform()
    for fam, top, bottom, has_next in family_spans(y):
        ax.plot(
            [FAMILY_BRACKET_X, FAMILY_BRACKET_X],
            [bottom + 0.1, top - 0.1],
            color=FAMILY_COLOR,
            lw=FAMILY_BRACKET_LW,
            solid_capstyle="butt",
            transform=trans,
            clip_on=False,
        )
        ax.text(
            FAMILY_BRACKET_X - FAMILY_LABEL_PAD,
            (top + bottom) / 2,
            fam,
            ha="right",
            va="center",
            fontsize=FAMILY_FS,
            color=FAMILY_COLOR,
            weight="bold",
            transform=trans,
            clip_on=False,
        )
        if has_next:
            ax.axhline(bottom, color=GRID, lw=0.8, zorder=0)

    squircle_bar_ends(ax, [r for r in outer if r is not None], horizontal=True)


def plot_prior(ax, y: list[float]) -> None:
    """Right panel: dot matrix of prior-benchmark coverage over the same rows."""
    xs = list(range(len(PRIOR_ORDER)))
    for xi, name in zip(xs, PRIOR_ORDER):
        for yi, (_, _, short) in zip(y, TAXONOMY):
            cnt = PRIOR[name].get(short, 0)
            if cnt > 0:
                ax.scatter(
                    xi,
                    yi,
                    s=DOT_SIZE_FILLED,
                    color=DOT_FILL,
                    edgecolor=SURFACE,
                    linewidth=1.2,
                    zorder=3,
                )
                ax.text(
                    xi + 0.24,
                    yi,
                    str(cnt),
                    va="center",
                    ha="left",
                    fontsize=VALUE_FS - 2,
                    color=SLATE,
                )
            else:
                ax.scatter(
                    xi,
                    yi,
                    s=DOT_SIZE_EMPTY,
                    facecolor=SURFACE,
                    edgecolor=SLATE_LIGHT,
                    linewidth=1.0,
                    zorder=3,
                )
    ax.set_xlim(-0.55, len(PRIOR_ORDER) - 0.2)
    ax.set_xticks(
        xs, [PRIOR_SHORT[n] for n in PRIOR_ORDER], fontsize=TICK_FS, color=INK
    )
    ax.xaxis.set_ticks_position("top")
    ax.tick_params(axis="x", length=0, pad=6)
    ax.tick_params(axis="y", length=0, labelleft=False)
    ax.grid(False)
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.set_facecolor(SURFACE)
    for _, _, bottom, has_next in family_spans(y):
        if has_next:
            ax.axhline(bottom, color=GRID, lw=0.8, zorder=0)


def make_figure(counts: Counter) -> plt.Figure:
    n = len(TAXONOMY)
    y = [float(n - 1 - i) for i in range(n)]  # first taxonomy row on top
    fig = plt.figure(figsize=FIG_SIZE)
    fig.patch.set_facecolor(SURFACE)
    gs = fig.add_gridspec(
        1,
        2,
        width_ratios=WIDTH_RATIOS,
        wspace=WSPACE,
        left=LEFT_MARGIN,
        right=0.98,
        top=0.90,
        bottom=0.07,
    )
    ax = fig.add_subplot(gs[0])
    axd = fig.add_subplot(gs[1], sharey=ax)
    plot_ours(ax, counts, y)
    plot_prior(axd, y)

    if PRIOR_PLACEHOLDER:
        fig.text(
            0.02,
            0.93,
            "PLACEHOLDER coverage for " + ", ".join(sorted(PRIOR_PLACEHOLDER)) + ".",
            fontsize=SUBTITLE_FS,
            color=SLATE,
            ha="left",
        )
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
        "--database-url", help="full connection string; bypasses config/config.yaml"
    )
    parser.add_argument(
        "--plot-dir",
        type=Path,
        default=PLOT_DIR,
        help=f"directory the figure is saved to as {PLOT_NAME}.png/.pdf (default: {PLOT_DIR})",
    )
    args = parser.parse_args()

    conn = psycopg2.connect(database_url(args))
    try:
        counts = fetch_counts(conn)
    finally:
        conn.close()
    if not counts:
        sys.exit("no non-deprecated tasks found")
    check_taxonomy(counts)

    per_type = Counter()
    for (model_type, _), n in counts.items():
        per_type[model_type] += n
    print(
        f"{OUR_NAME}: {sum(per_type.values())} tasks over {len(per_type)} of "
        f"{sum(1 for _, t, _ in TAXONOMY if t)} taxonomy types"
    )
    for fam, model_type, short in TAXONOMY:
        if model_type:
            by_diff = {
                d: counts[(model_type, d)]
                for d in DIFFICULTY_ORDER
                if counts[(model_type, d)]
            }
            print(
                f"  {fam:<20}{short:<28}{per_type[model_type]:>3}  {by_diff or 'not covered'}"
            )

    fig = make_figure(counts)
    for path in save_figure(fig, args.plot_dir):
        print(f"wrote {path}")
    plt.close(fig)


if __name__ == "__main__":
    main()
