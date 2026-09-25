#!/usr/bin/env python
"""Plot the difficulty distribution of the v2 task set as a half-column paper figure.

Reads the live `tasks` table of the v2 Neon database (config database.v2_url)
over the non-deprecated tasks and produces difficulty.{png,pdf} under
operation/results/v2/plots/difficulty/: one bar per
tasks.human_difficulty_measure bucket, easiest to hardest, count at the tip,
with a task-count axis on the left.

The canvas is the printed size (2.7 in wide, half of style_guide.yaml
print.textwidth_in), so the font sizes in the FIGURE STYLE block are the sizes
that print; include it at width=0.49\\textwidth and do not scale it. A wrapfigure
snippet that does so, with body text flowing beside it, is printed to stdout
after the counts. Colours, typeface,
and the bar helpers are shared with plot_difficulty_and_types.py in this
directory. Run with the shared plotting environment, ~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_difficulty.py
    python operation/v2/paper_scripts/plot_difficulty.py --plot-dir /tmp/plots
    python operation/v2/paper_scripts/plot_difficulty.py --database-url postgresql://...
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    DIFFICULTY_COLORS,
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
    squircle_bar_ends,
    tick_top,
)

PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "difficulty"
PLOT_NAME = "difficulty"
TEX_FIGURE_PATH = f"./figures/{PLOT_NAME}.pdf"  # where the paper keeps its figures

# ============================================================================
# FIGURE STYLE
# ============================================================================
# The canvas is the printed size (half of style_guide.yaml print.textwidth_in,
# included in a 0.49\textwidth wrapfigure), so every font size below is the size that prints.
PRINT = STYLE["print"]
FIG_SIZE = (PRINT["textwidth_in"] / 2, 1.7)
SAVE_DPI = 300
SAVE_PAD_IN = PRINT["save_pad_in"]  # tight-bbox padding around the saved figure
TICK_FS = PRINT["pt"]["label"]  # bucket names under the bars
BUCKET_WEIGHT = PRINT["legend_weight"]  # bucket names name the colours, as a legend would
VALUE_FS = PRINT["pt"]["label"]  # counts at the bar tips
YTICK_STEP = 10  # tasks between y ticks
BAR_WIDTH = 0.62
VALUE_GAP = 0.02  # bar tip to count, as a share of the tallest bar
HEADROOM = 1.13  # the y-limit clears the tallest bar by this factor, then rounds up to a tick

plt.rcParams["axes.unicode_minus"] = False

SQL = """
    SELECT human_difficulty_measure
    FROM tasks
    WHERE deprecated IS NOT TRUE
"""

# A wrapfigure (\usepackage{wrapfig}) so body text flows beside the half-column
# figure. Place it right after a paragraph break, not inside a list or float.
TEX_TEMPLATE = r"""% needs \usepackage{wrapfig} in the preamble
\begin{wrapfigure}{r}{0.49\textwidth}
\vspace{-\baselineskip}
\centering
\includegraphics[width=\linewidth]{FIGURE_PATH}
\caption{
Difficulty distribution of the TASK_COUNT tasks in \benchmarkname.
}
\label{fig:difficulty}
\vspace{-\baselineskip}
\end{wrapfigure}"""


def fetch_counts(conn) -> Counter:
    """Counter of human_difficulty_measure over the non-deprecated tasks."""
    with conn.cursor() as cur:
        cur.execute(SQL)
        return Counter(difficulty for (difficulty,) in cur.fetchall())


def tick_label(bucket) -> str:
    """'Medium-Hard' -> 'Medium-\\nHard' so four labels fit under a half column."""
    return str(bucket).replace("-", "-\n")


def make_figure(counts: Counter) -> plt.Figure:
    labels = sorted(counts, key=order_key(DIFFICULTY_ORDER))
    values = [counts[d] for d in labels]
    colors = [DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR) for d in labels]
    tallest = max(values)

    fig, ax = plt.subplots(figsize=FIG_SIZE)
    fig.patch.set_facecolor(SURFACE)
    x = range(len(labels))
    bars = ax.bar(x, values, width=BAR_WIDTH, color=colors, linewidth=0, zorder=2)
    for xi, n in zip(x, values):
        ax.text(
            xi, n + tallest * VALUE_GAP, str(n),
            ha="center", va="bottom", fontsize=VALUE_FS, color=INK,
        )

    quiet_axes(ax)
    ax.set_xticks(list(x), [tick_label(d) for d in labels], fontsize=TICK_FS, color=INK,
                  fontweight=BUCKET_WEIGHT)
    ax.set_xlim(-0.5 - (1 - BAR_WIDTH) / 2, len(labels) - 0.5 + (1 - BAR_WIDTH) / 2)
    top, _ = tick_top(tallest * HEADROOM, YTICK_STEP)
    ax.set_ylim(0, top)
    ax.set_yticks(range(0, top + 1, YTICK_STEP))
    axis_title(ax, "Tasks")
    fig.tight_layout(pad=0)  # the tight bbox on save sets the border, not the layout
    squircle_bar_ends(ax, bars, horizontal=False)
    return fig


def save_figure(fig: plt.Figure, plot_dir: Path) -> list[Path]:
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "pdf"):
        path = plot_dir / f"{PLOT_NAME}.{ext}"
        fig.savefig(
            path,
            dpi=SAVE_DPI,
            bbox_inches="tight",
            pad_inches=SAVE_PAD_IN,
            facecolor=SURFACE,
        )
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

    total = sum(counts.values())
    print(f"{total} non-deprecated tasks by human_difficulty_measure:")
    for d in sorted(counts, key=order_key(DIFFICULTY_ORDER)):
        print(f"  {d:<14}{counts[d]:>4}{counts[d] / total:>8.1%}")

    fig = make_figure(counts)
    for path in save_figure(fig, args.plot_dir):
        print(f"wrote {path}")
    plt.close(fig)

    print(f"\nLaTeX (copy {PLOT_NAME}.pdf to {TEX_FIGURE_PATH} in the paper):\n")
    print(
        TEX_TEMPLATE.replace("FIGURE_PATH", TEX_FIGURE_PATH).replace(
            "TASK_COUNT", str(total)
        )
    )


if __name__ == "__main__":
    main()
