#!/usr/bin/env python
"""Deprecated: the paper figure is now plot_difficulty.py (difficulty only, half
column). This file stays for its shared style helpers, which plot_difficulty.py
and plot_task_coverage.py import.

Plot and describe the v2 task set: difficulty, task type, expected solve time.

Reads the live `tasks` table of the v2 Neon database (config database.v2_url)
over the non-deprecated tasks and produces:

  A figure, difficulty_and_types.{png,pdf}, saved under
  operation/results/v2/plots/difficulty_and_types/:
    Left   -- difficulty distribution: task count per
              tasks.human_difficulty_measure bucket, easiest to hardest.
    Right  -- task type distribution: task count per
              case_classification.model_type, largest first, each bar stacked
              by difficulty in the same colours as the left panel.

  A text report with three sections (same numbers also written as JSON to
  operation/results/v2/task_analysis/<timestamp>.json, a fresh file per run):
    1. Difficulty distribution, plus mean / min / max of
       case_classification.difficulty_score inside each bucket.
    2. Task type distribution, cross-tabbed against difficulty.
    3. Expected solve time: case_classification.time_assumption_h (human
       estimate, every task) summary stats, time_range histogram, and
       mean / median per difficulty and per model type;
       tasks.ai_time_estimate_min (AI estimate, only the tasks that have one)
       summarised alongside. Not plotted.

Colours and the typeface come from operation/v2/style_guide.yaml. Run with the
shared plotting environment, ~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_difficulty_and_types.py
    python operation/v2/paper_scripts/plot_difficulty_and_types.py --out /tmp/task_analysis.json
    python operation/v2/paper_scripts/plot_difficulty_and_types.py --plot-dir /tmp/plots
    python operation/v2/paper_scripts/plot_difficulty_and_types.py --database-url postgresql://...
"""

import argparse
import importlib.util
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

import matplotlib
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import psycopg2
import yaml
from fontTools.ttLib import TTFont
from matplotlib import font_manager
from matplotlib.legend_handler import HandlerPatch

# This file lives at <repo>/operation/v2/paper_scripts/plot_difficulty_and_types.py,
# so the repo root is four levels up. Outputs go under the shared operation/results/.
REPO_ROOT = Path(__file__).resolve().parents[3]
STYLE_PATH = REPO_ROOT / "operation" / "v2" / "style_guide.yaml"
RESULTS_DIR = REPO_ROOT / "operation" / "results" / "v2" / "task_analysis"
PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "difficulty_and_types"
PLOT_NAME = "difficulty_and_types"
RUN_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, and safe as a filename

# tasks.human_difficulty_measure buckets, easiest first. Unknown values sort last.
DIFFICULTY_ORDER = ("Easy", "Medium", "Medium-Hard", "Hard")

# ============================================================================
# FIGURE STYLE
# ============================================================================
# Colours and typeface live in style_guide.yaml so every v2 figure shares them.
STYLE = yaml.safe_load(STYLE_PATH.read_text())
DIFFICULTY_COLORS: dict[str, str] = STYLE["difficulty"]
UNKNOWN_COLOR = STYLE["ink"]["unknown"]
INK = STYLE["ink"]["primary"]
GRID = STYLE["ink"]["grid"]
BASELINE = STYLE["ink"]["baseline"]
SURFACE = STYLE["ink"]["surface"]

# Value axes, shared by every paper figure: style_guide.yaml `axis`.
AXIS = STYLE["axis"]
AXIS_INK = STYLE["ink"][AXIS["ink"]]
AXIS_TICK_FS = AXIS["tick_pt"]
AXIS_TITLE_FS = AXIS["title_pt"]
AXIS_TITLE_WEIGHT = AXIS["title_weight"]

FIG_SIZE = (18, 8)
WIDTH_RATIOS = [1, 1.7]  # [difficulty, model types]
WSPACE = 0.55  # room for the tilted model-type labels on the right panel
XLABEL_PAD = 12  # points between the right panel's tick numbers and its axis title
SAVE_DPI = 200
TITLE_FS = 22
LABEL_FS = 18
TICK_FS = 16
VALUE_FS = 16
LEGEND_FS = 16
TYPE_LABEL_ROTATION = 25  # degrees; tilts the long model-type names

# Bar ends are squircles; the trick and its numbers are in style_guide.yaml `bar_end`.
BAR_ROUND_PT = STYLE["bar_end"]["round_pt"]
SQUIRCLE_N = STYLE["bar_end"]["squircle_n"]
SQUIRCLE_STEPS = STYLE["bar_end"]["steps"]  # points per corner
LEGEND_HANDLE_ROUND_PT = STYLE["bar_end"]["legend_round_pt"]


def register_font_faces(family: str, faces: list[int]) -> None:
    """matplotlib reads only face 0 of a .ttc, so bold and italic fall back to it.
    Split the listed faces into cached .ttf files and register them."""
    path = Path(font_manager.findfont(family, fallback_to_default=False))
    if path.suffix.lower() != ".ttc":
        return
    out_dir = Path(matplotlib.get_cachedir()) / "ttc_faces"
    out_dir.mkdir(parents=True, exist_ok=True)
    for index in faces:
        face = out_dir / f"{path.stem}-{index}.ttf"
        if not face.exists():
            TTFont(path, fontNumber=index).save(face)
        font_manager.fontManager.addfont(str(face))


try:
    register_font_faces(STYLE["font"], STYLE.get("font_faces", []))
except ValueError:  # font not installed: fall back to sans-serif below
    pass
plt.rcParams["font.family"] = [STYLE["font"], "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

# case_classification.time_range buckets, shortest first. Unknown values sort last.
TIME_RANGE_ORDER = (
    "30min-1h", "1-2h", "2-3h", "3-4h", "4-5h", "5-7h", "7-9h", "9-12h",
    "12-16h", "16-20h", "20-25h", "25-30h", "30-35h",
)

SQL = """
    SELECT id, task_name, human_difficulty_measure, case_classification,
           ai_time_estimate_min
    FROM tasks
    WHERE deprecated IS NOT TRUE
    ORDER BY id
"""


def load_config() -> dict:
    """Load the merged config via the vendored loader in config/python."""
    config_dir = REPO_ROOT / "config"
    spec = importlib.util.spec_from_file_location(
        "_mbabench_config", config_dir / "python" / "config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config.load(config_dir).as_dict()


def database_url(args) -> str:
    if args.database_url:
        return args.database_url
    url = load_config().get("database", {}).get("v2_url")
    if not url:
        sys.exit("config/config.yaml has no database.v2_url — pass --database-url.")
    return url


def fetch_tasks(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL)
        rows = cur.fetchall()
    tasks = []
    for task_id, name, difficulty, classification, ai_min in rows:
        cls = classification or {}
        tasks.append(
            {
                "id": task_id,
                "task_name": name,
                "difficulty": difficulty,
                "difficulty_score": cls.get("difficulty_score"),
                "model_type": cls.get("model_type"),
                "time_range": cls.get("time_range"),
                "time_h": cls.get("time_assumption_h"),
                "ai_time_min": ai_min,
            }
        )
    return tasks


def order_key(order: tuple[str, ...]):
    """Sort known labels by their position in `order`; unknown ones after, A-Z."""
    rank = {label: i for i, label in enumerate(order)}
    return lambda label: (rank.get(label, len(order)), str(label))


def summarize(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def pct(p):  # linear interpolation, same as numpy's default
        pos = (len(ordered) - 1) * p
        lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)

    return {
        "n": len(values),
        "mean": round(mean(values), 3),
        "median": round(median(values), 3),
        "p25": round(pct(0.25), 3),
        "p75": round(pct(0.75), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "total": round(sum(values), 3),
    }


def count_table(tasks: list[dict], key: str, order=None) -> dict:
    counts = Counter(t[key] for t in tasks)
    labels = sorted(counts, key=order_key(order) if order else lambda l: -counts[l])
    total = len(tasks)
    return {
        label: {"count": counts[label], "share": round(counts[label] / total, 4)}
        for label in labels
    }


def difficulty_distribution(tasks: list[dict]) -> dict:
    table = count_table(tasks, "difficulty", DIFFICULTY_ORDER)
    for label, cell in table.items():
        scores = [t["difficulty_score"] for t in tasks if t["difficulty"] == label]
        cell["difficulty_score"] = summarize(scores)
    return table


def task_type_distribution(tasks: list[dict]) -> dict:
    table = count_table(tasks, "model_type")
    for label, cell in table.items():
        subset = [t for t in tasks if t["model_type"] == label]
        by_diff = Counter(t["difficulty"] for t in subset)
        cell["by_difficulty"] = {
            d: by_diff[d] for d in sorted(by_diff, key=order_key(DIFFICULTY_ORDER))
        }
        cell["mean_time_h"] = summarize([t["time_h"] for t in subset]).get("mean")
    return table


def group_time(tasks: list[dict], key: str, order=None) -> dict:
    groups = defaultdict(list)
    for t in tasks:
        groups[t[key]].append(t)
    labels = sorted(groups, key=order_key(order) if order else lambda l: -len(groups[l]))
    return {
        label: {
            "human_h": summarize([t["time_h"] for t in groups[label]]),
            "ai_min": summarize([t["ai_time_min"] for t in groups[label]]),
        }
        for label in labels
    }


def expected_solve_time(tasks: list[dict]) -> dict:
    return {
        "human_h": summarize([t["time_h"] for t in tasks]),
        "ai_min": summarize([t["ai_time_min"] for t in tasks]),
        "time_range": count_table(
            [t for t in tasks if t["time_range"]], "time_range", TIME_RANGE_ORDER
        ),
        "by_difficulty": group_time(tasks, "difficulty", DIFFICULTY_ORDER),
        "by_model_type": group_time(tasks, "model_type"),
    }


# --- report ------------------------------------------------------------------


def bar(share: float, width: int = 30) -> str:
    return "#" * round(share * width)


def fmt(value, digits=1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def abbreviate(label: str) -> str:
    """Short column header: 'Medium' -> 'Med', 'Medium-Hard' -> 'Med-Hd'."""
    first, *rest = str(label).split("-")
    return "-".join([first[:3]] + [p[:2] for p in rest])


def print_report(report: dict) -> None:
    n = report["task_count"]
    print(f"v2 task set: {n} non-deprecated tasks  (generated {report['generated_at']})")

    print("\n== 1. Difficulty distribution (tasks.human_difficulty_measure) ==")
    print(f"{'difficulty':<14}{'n':>4}{'share':>8}  {'score mean':>10}{'min':>6}{'max':>6}  ")
    for label, cell in report["difficulty"].items():
        s = cell["difficulty_score"]
        print(
            f"{label:<14}{cell['count']:>4}{cell['share']:>8.1%}  "
            f"{fmt(s.get('mean'), 2):>10}{fmt(s.get('min'), 2):>6}{fmt(s.get('max'), 2):>6}  "
            f"{bar(cell['share'])}"
        )

    print("\n== 2. Task type distribution (case_classification.model_type) ==")
    diffs = list(report["difficulty"])
    head = "".join(f"{abbreviate(d):>7}" for d in diffs)
    print(f"{'model_type':<40}{'n':>4}{'share':>8}{head}  {'mean h':>7}")
    for label, cell in report["task_type"].items():
        cross = "".join(f"{cell['by_difficulty'].get(d, 0):>7}" for d in diffs)
        print(
            f"{label:<40}{cell['count']:>4}{cell['share']:>8.1%}{cross}  "
            f"{fmt(cell['mean_time_h']):>7}"
        )

    t = report["solve_time"]
    print("\n== 3. Expected solve time ==")
    print("human estimate, case_classification.time_assumption_h (hours):")
    print("  " + "  ".join(f"{k}={v}" for k, v in t["human_h"].items()))
    print("AI estimate, tasks.ai_time_estimate_min (minutes):")
    print("  " + "  ".join(f"{k}={v}" for k, v in t["ai_min"].items()))

    print("\ntime_range histogram:")
    for label, cell in t["time_range"].items():
        print(f"  {label:<10}{cell['count']:>4}{cell['share']:>8.1%}  {bar(cell['share'])}")

    for title, key in (("by difficulty", "by_difficulty"), ("by model type", "by_model_type")):
        print(f"\n{title}:")
        print(f"  {'':<40}{'n':>4}{'human mean h':>13}{'median h':>10}{'ai n':>6}{'ai mean min':>12}")
        for label, cell in t[key].items():
            h, a = cell["human_h"], cell["ai_min"]
            print(
                f"  {label:<40}{h['n']:>4}{fmt(h.get('mean')):>13}{fmt(h.get('median')):>10}"
                f"{a['n']:>6}{fmt(a.get('mean')):>12}"
            )


# --- figure ------------------------------------------------------------------


def quiet_axes(ax, value_axis: str = "y") -> None:
    """Value-axis chrome for the paper figures (style_guide.yaml `axis`).

    The bottom baseline only, a hairline grid on the value axis, its tick numbers in
    the axis ink. value_axis is "y", or "both" for a scatter whose x is numeric too.
    """
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.grid(axis=value_axis, color=GRID, linewidth=AXIS["grid_lw"], zorder=0)
    ax.set_axisbelow(True)
    numbers = dict(labelsize=AXIS_TICK_FS, labelcolor=AXIS_INK)
    ax.tick_params(axis="y", length=0, pad=AXIS["tick_pad_pt"], **numbers)
    ax.tick_params(axis="x", length=0, pad=AXIS["tick_pad_pt"], **(numbers if value_axis == "both" else {}))


def axis_title(ax, text: str, axis: str = "y") -> None:
    """A value-axis title: slate, a step above the tick numbers, Avenir Medium."""
    set_label = ax.set_ylabel if axis == "y" else ax.set_xlabel
    set_label(text, fontsize=AXIS_TITLE_FS, color=AXIS_INK, fontweight=AXIS_TITLE_WEIGHT,
              labelpad=AXIS["title_pad_pt"])


def tick_top(peak: float, step: int, max_ticks: int | None = None) -> tuple[int, int]:
    """(top, step) of a value axis from 0: top is the first tick at or above peak, so the
    grid closes the panel; the step doubles until there are at most max_ticks ticks."""
    while True:
        top = max(step, math.ceil(peak / step) * step)
        if max_ticks is None or top // step + 1 <= max_ticks:
            return top, step
        step *= 2


def style_axis(ax, horizontal: bool) -> None:
    """Recessive chrome: baseline only, hairline grid on the value axis."""
    for side in ("top", "right", "left" if horizontal else "top"):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color(BASELINE)
    ax.tick_params(colors=AXIS_INK, labelsize=TICK_FS, length=0)
    ax.grid(axis="x" if horizontal else "y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)


def _squircle_arc(cx, cy, r, a0, a1):
    """Points on a quarter superellipse of radius r centred at (cx, cy).

    Angles in degrees, swept a0 -> a1. Exponent SQUIRCLE_N shapes the corner:
    2 is a circle, 4-5 is Apple's continuous corner.
    """
    k = 2.0 / SQUIRCLE_N
    pts = []
    for i in range(SQUIRCLE_STEPS + 1):
        t = math.radians(a0 + (a1 - a0) * i / SQUIRCLE_STEPS)
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + r * math.copysign(abs(c) ** k, c), cy + r * math.copysign(abs(s) ** k, s)))
    return pts


def _squircle_end_path(x0, y0, x1, y1, r, horizontal: bool):
    """Pixel-space outline of a bar whose data end (top or right) is squircled."""
    if horizontal:  # rounded right end
        pts = [(x0, y0), (x1 - r, y0)]
        pts += _squircle_arc(x1 - r, y0 + r, r, -90, 0)
        pts += _squircle_arc(x1 - r, y1 - r, r, 0, 90)
        pts += [(x0, y1)]
    else:  # rounded top
        pts = [(x0, y0), (x1, y0)]
        pts += _squircle_arc(x1 - r, y1 - r, r, 0, 90)
        pts += _squircle_arc(x0 + r, y1 - r, r, 90, 180)
    return pts  # Polygon(closed=True) draws the baseline edge back to the start


def squircle_bar_ends(ax, rects, horizontal: bool) -> None:
    """Replace each rectangle with a copy whose data end has squircle corners.

    Must run after the axis limits are final: the point radius is converted to
    pixels and back through transData, which only settles then.
    """
    to_px, to_data = ax.transData, ax.transData.inverted()
    r_max = BAR_ROUND_PT * ax.figure.dpi / 72.0
    for rect in rects:
        if rect.get_width() <= 0 or rect.get_height() <= 0:
            continue
        (x0, y0), (x1, y1) = to_px.transform(
            [(rect.get_x(), rect.get_y()),
             (rect.get_x() + rect.get_width(), rect.get_y() + rect.get_height())]
        )
        x0, x1 = sorted((x0, x1))  # an inverted axis (rows top-down) flips the pixel order
        y0, y1 = sorted((y0, y1))
        thickness = (y1 - y0) if horizontal else (x1 - x0)
        r = min(r_max, thickness / 2)
        pts = to_data.transform(_squircle_end_path(x0, y0, x1, y1, r, horizontal))
        ax.add_patch(
            mpatches.Polygon(
                pts, closed=True, facecolor=rect.get_facecolor(),
                edgecolor=rect.get_edgecolor(), linewidth=rect.get_linewidth(),
                zorder=rect.get_zorder(),
            )
        )
        rect.remove()


class _RoundedHandler(HandlerPatch):
    """Legend swatch drawn as a rounded rectangle to match the bar ends."""

    def create_artists(
        self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans
    ):
        patch = mpatches.FancyBboxPatch(
            (-xdescent, -ydescent), width, height,
            boxstyle=f"round,pad=0,rounding_size={LEGEND_HANDLE_ROUND_PT}",
            facecolor=orig_handle.get_facecolor(), edgecolor="none", linewidth=0,
        )
        patch.set_transform(trans)
        return [patch]


def plot_difficulty(ax, difficulty: dict) -> None:
    labels = list(difficulty)
    counts = [difficulty[d]["count"] for d in labels]
    colors = [DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR) for d in labels]
    bars = ax.bar(labels, counts, width=0.62, color=colors, linewidth=0, zorder=2)
    for rect, n in zip(bars, counts):
        ax.text(
            rect.get_x() + rect.get_width() / 2,
            rect.get_height() + max(counts) * 0.015,
            str(n), ha="center", va="bottom", fontsize=VALUE_FS, color=INK,
        )
    style_axis(ax, horizontal=False)
    ax.tick_params(axis="x", labelcolor=INK)  # category labels in ink, like the right panel
    ax.set_ylim(0, max(counts) * 1.15)
    ax.set_ylabel("Tasks", fontsize=LABEL_FS, color=AXIS_INK, fontweight=AXIS_TITLE_WEIGHT)
    ax.set_title("Difficulty distribution", fontsize=TITLE_FS, color=INK, pad=14)
    squircle_bar_ends(ax, bars, horizontal=False)


def plot_task_types(ax, task_type: dict, difficulty_labels: list[str]) -> None:
    labels = list(task_type)[::-1]  # largest bar on top
    y = range(len(labels))
    left = [0] * len(labels)
    outer = [None] * len(labels)  # the last non-empty segment of each bar
    handles = []
    for d in difficulty_labels:
        widths = [task_type[t]["by_difficulty"].get(d, 0) for t in labels]
        bars = ax.barh(
            y, widths, left=left, height=0.68, label=d,
            color=DIFFICULTY_COLORS.get(d, UNKNOWN_COLOR),
            edgecolor=SURFACE, linewidth=1.0, zorder=2,
        )
        handles.append(bars)
        for i, (rect, w) in enumerate(zip(bars, widths)):
            if w > 0:
                outer[i] = rect
        left = [a + b for a, b in zip(left, widths)]
    biggest = max(left)
    for yi, total in zip(y, left):
        ax.text(
            total + biggest * 0.015, yi, str(total),
            ha="left", va="center", fontsize=VALUE_FS, color=INK,
        )
    style_axis(ax, horizontal=True)
    ax.set_yticks(
        list(y), labels, fontsize=TICK_FS, color=INK,
        rotation=TYPE_LABEL_ROTATION, ha="right", va="center", rotation_mode="anchor",
    )
    ax.set_xlim(0, biggest * 1.12)
    ax.set_xlabel("Tasks", fontsize=LABEL_FS, color=AXIS_INK, fontweight=AXIS_TITLE_WEIGHT,
                  labelpad=XLABEL_PAD)
    ax.set_title("Task type distribution", fontsize=TITLE_FS, color=INK, pad=14)
    ax.legend(
        handles=[h[0] for h in handles], labels=difficulty_labels, title="Difficulty",
        loc="lower right", fontsize=LEGEND_FS, title_fontsize=LEGEND_FS,
        frameon=False, labelcolor=INK,
        handler_map={mpatches.Rectangle: _RoundedHandler()},
    )
    squircle_bar_ends(ax, [r for r in outer if r is not None], horizontal=True)


def make_figure(report: dict) -> plt.Figure:
    fig, (ax_left, ax_right) = plt.subplots(
        1, 2, figsize=FIG_SIZE, gridspec_kw={"width_ratios": WIDTH_RATIOS, "wspace": WSPACE}
    )
    fig.patch.set_facecolor(SURFACE)
    plot_difficulty(ax_left, report["difficulty"])
    plot_task_types(ax_right, report["task_type"], list(report["difficulty"]))
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
        "--out",
        type=Path,
        default=None,
        help=f"JSON output path (default: {RESULTS_DIR}/<timestamp>.json)",
    )
    parser.add_argument(
        "--no-json", action="store_true", help="only print the report; write nothing"
    )
    parser.add_argument(
        "--plot-dir",
        type=Path,
        default=PLOT_DIR,
        help=f"directory the figure is saved to as {PLOT_NAME}.png/.pdf "
        f"(default: {PLOT_DIR})",
    )
    parser.add_argument(
        "--no-plot", action="store_true", help="skip the figure"
    )
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    conn = psycopg2.connect(database_url(args))
    try:
        tasks = fetch_tasks(conn)
    finally:
        conn.close()
    if not tasks:
        sys.exit("no non-deprecated tasks found")

    report = {
        "generated_at": started.isoformat(timespec="seconds"),
        "task_count": len(tasks),
        "difficulty": difficulty_distribution(tasks),
        "task_type": task_type_distribution(tasks),
        "solve_time": expected_solve_time(tasks),
        "tasks": tasks,
    }
    print_report(report)

    if not args.no_json:
        out = args.out or RESULTS_DIR / f"{started.strftime(RUN_STAMP_FORMAT)}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2))
        print(f"\nwrote {out}")

    if not args.no_plot:
        fig = make_figure(report)
        for path in save_figure(fig, args.plot_dir):
            print(f"wrote {path}")
        plt.close(fig)


if __name__ == "__main__":
    main()
