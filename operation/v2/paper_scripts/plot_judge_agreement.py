#!/usr/bin/env python
"""Judge agreement with human annotation, by rubric category and attempt.

Reads public.judge_annotations (human TP/FP/TN/FN label per "Category::Check"),
joined to gradings / task_attempts / tasks, and draws one figure under
operation/results/v2/plots/judge_agreement/judge_agreement.{png,pdf}:

  rows     the 12 rubric_9 categories, in rubric order, with their check count
  columns  annotated attempts, grouped by agent (rounded header pill) with the
           task id under it
  cell     a stacked pill of that attempt's checks in the category, from the
           judge's side: blue = judge agreed with the annotator, orange = judge
           was wrong; dark = the judge said pass, pale = the judge said fail.

Positive class is "check failed": TP both say fail, FN judge missed a fail,
FP judge failed a passing check. Judge v2 gradings (earlier check set) are skipped.

Colours, typeface and helpers come from style_guide.yaml and the sibling
plot_difficulty_and_types.py. Run with ~/.uv/uv_venvs/base:
    python operation/v2/paper_scripts/plot_judge_agreement.py
"""

import importlib.util
import json
import sys
from collections import Counter
from itertools import groupby
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import psycopg2
import psycopg2.extras
from matplotlib.patches import FancyBboxPatch, Patch, Rectangle

# This file lives at <repo>/operation/v2/paper_scripts/, so the repo root is four levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]
from plot_difficulty_and_types import INK, MUTED, SURFACE, _RoundedHandler  # noqa: E402

RUBRIC = json.loads((REPO_ROOT / "judge" / "prompts" / "rubrics" / "rubric_9.json").read_text())
OUT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "judge_agreement"
PLOT_NAME = "judge_agreement"
SAVE_DPI = 200
MIN_JUDGE_VERSION = 3

# Everything is from the judge's side: hue = judge right (blue) or wrong (orange),
# intensity = the judge's verdict (dark = judge said pass, pale = judge said fail).
COLORS = {
    "TN": "#2a78d6",  # judge right, said pass
    "TP": "#b9d3f3",  # judge right, said fail
    "FP": "#f2a985",  # judge wrong, said fail
    "FN": "#c2410c",  # judge wrong, said pass
}
SEGMENT_ORDER = ("TN", "TP", "FP", "FN")  # agreed first, errors at the right end
LEGEND_ORDER = ("TP", "FP", "TN", "FN")  # column-major fill of a 2x2 grid: TP TN / FP FN

# Sized for a conference text width (~7 in): type is set at paper size, not scaled down.
# Height follows from the row pitch and the measured height of the tilted task names.
FIG_W = 7.2
ROW_IN = 0.27  # inches per category row
LEFT_PAD_IN = 0.12  # gap between the widest category label and the first column
MARGIN_IN = 0.06
CAT_FS = 9
CAT_ROTATION = 25  # degrees; tilted like the task-type labels in the other v2 figures
TASK_FS = 8
HEADER_FS = 7
LEGEND_FS_PT = 11
LEGEND_DROP = 0.04  # figure fraction the legend sits below the centre of its block
BAR_H = 0.46  # row pitch is 1
BAR_W = 0.84  # column pitch is 1
SEGMENT_GAP_PT = 0.8  # surface hairline between segments
CHIP_H = 0.6  # task-id chip height, rows
CHIP_W = 0.56  # task-id chip width, columns
CHIP_FILL = "#f1f0eb"
TASK_Y = -0.5 - 0.18 - CHIP_H / 2  # task-id row centre (rows grow downward)
HEADER_GAP = 0.22  # rows between the id chips and the agent pill
ROW_LABEL_FS = 8  # "Agent" / "Task ID" row names at the left
HEADER_H_ONE = 0.62  # pill height, rows, when every agent name fits on one line
HEADER_H_TWO = 1.05  # ... when some name had to wrap
HEADER_FILL = "#edece6"
SHORT_CAT = {"Model Outputs & Executive Summary": "Model Outputs & Exec. Summary"}

CATEGORY_ORDER = list(RUBRIC)
CHECKS = [(cat, c["name"]) for cat in CATEGORY_ORDER for c in RUBRIC[cat]]
CHECK_INDEX = {f"{cat}::{name}": i for i, (cat, name) in enumerate(CHECKS)}
CAT_SPANS: dict[str, list[int]] = {}
for i, (cat, _) in enumerate(CHECKS):
    CAT_SPANS.setdefault(cat, [i, i])[1] = i
SHORT_AGENT = {
    "claude_fable_5_1_cowork_max": "Fable 5.1 Cowork",
    "chatgpt_gpt_6_astra_work_ultra": "GPT-6 Astra Work",
    "codex_openai/gpt-5.6-sol-xhigh": "Codex gpt-5.6-sol",
    "openpyxl_anthropic/claude-fable-5-max": "openpyxl Fable 5",
    "claude_opus_4_8": "Opus 4.8",
}


# --- data ----------------------------------------------------------------------


def db_url() -> str:
    spec = importlib.util.spec_from_file_location("_cfg", REPO_ROOT / "config" / "python" / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Config.load(REPO_ROOT / "config").as_dict()["database"]["v2_url"]


def fetch() -> list[dict]:
    """One row per annotated grading (latest revision), sorted by agent then task."""
    conn = psycopg2.connect(db_url())
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        select distinct on (a.grading_id)
               a.grading_id, a.attempt_id, a.labels, g.judge_version, ta.agent_model_name,
               t.id as task_id, t.task_name
        from judge_annotations a
        join gradings g on g.id = a.grading_id
        join task_attempts ta on ta.id = a.attempt_id
        join tasks t on t.id = ta.task_id
        order by a.grading_id, a.revision desc, a.created_at desc
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    kept = [r for r in rows if r["judge_version"] >= MIN_JUDGE_VERSION]
    for r in rows:
        if r not in kept:
            print(f"skipping grading {r['grading_id']} (judge v{r['judge_version']}, {len(r['labels'])} labels)")
    for r in kept:
        r["agent"] = SHORT_AGENT.get(r["agent_model_name"], r["agent_model_name"])
    return sorted(kept, key=lambda r: (r["agent"], r["task_id"]))


def cell_counts(row: dict, cat: str) -> Counter:
    a, b = CAT_SPANS[cat]
    keys = {f"{c}::{n}" for c, n in CHECKS[a:b + 1]}
    return Counter(v for k, v in row["labels"].items() if k in keys and v in COLORS)


# --- figure ----------------------------------------------------------------------


def pill(ax, x0, y0, w, h, **style) -> FancyBboxPatch:
    """A fully rounded box in data units whose ends are circular on screen.

    The axes' limits must be final: the x/y pixel scales are read off transData.
    """
    to_px = ax.transData.transform
    px_per_x = to_px((1, 0))[0] - to_px((0, 0))[0]
    px_per_y = abs(to_px((0, 1))[1] - to_px((0, 0))[1])
    r = (h / 2) * px_per_y / px_per_x  # half the height, expressed in x units
    return FancyBboxPatch(
        (x0, y0), w, h,
        boxstyle=f"round,pad=0,rounding_size={r}", mutation_aspect=px_per_x / px_per_y,
        **style,
    )


def draw_cell(ax, xi, yi, counts: Counter) -> None:
    """One slim pill per cell; segments are clipped to the pill so its ends stay round."""
    n = sum(counts.values())
    if n == 0:
        return
    x0, y0 = xi - BAR_W / 2, yi - BAR_H / 2
    clip = pill(ax, x0, y0, BAR_W, BAR_H, facecolor="none", edgecolor="none")
    clip.set_transform(ax.transData)
    left = x0
    for cls in SEGMENT_ORDER:
        w = BAR_W * counts[cls] / n
        if w <= 0:
            continue
        seg = Rectangle((left, y0), w, BAR_H, facecolor=COLORS[cls], edgecolor="none", zorder=2)
        seg.set_clip_path(clip)
        ax.add_patch(seg)
        if left > x0:  # hairline between segments, drawn in the surface colour
            ax.plot([left, left], [y0, y0 + BAR_H], color=SURFACE, linewidth=SEGMENT_GAP_PT,
                    solid_capstyle="butt", zorder=3)
        left += w


def text_size_in(fig, text) -> tuple[float, float]:
    """Rendered width and height of a Text artist, in inches."""
    bbox = text.get_window_extent(fig.canvas.get_renderer())
    return bbox.width / fig.dpi, bbox.height / fig.dpi


def wrap_to_width(fig, ax, label: str, width_in: float) -> str:
    """Break an agent name at the space nearest its middle if it overflows its pill."""
    probe = ax.text(0, 0, label, fontsize=HEADER_FS)
    w, _ = text_size_in(fig, probe)
    probe.remove()
    if w <= width_in or " " not in label:
        return label
    cut = min((i for i, ch in enumerate(label) if ch == " "), key=lambda i: abs(i - len(label) / 2))
    return label[:cut] + "\n" + label[cut + 1:]


def make_figure(rows: list[dict]) -> plt.Figure:
    n_att = len(rows)
    cats = CATEGORY_ORDER
    fig = plt.figure(figsize=(FIG_W, 4), facecolor=SURFACE)  # height is set once the header is measured
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(SURFACE)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    ax.set_xticks([])
    ax.set_xlim(-0.5, n_att - 0.5)

    # tilted category labels; the left margin is whatever the widest one needs
    ax.set_yticks(range(len(cats)))
    ax.set_yticklabels(
        [f"{SHORT_CAT.get(c, c)}  ({CAT_SPANS[c][1] - CAT_SPANS[c][0] + 1})" for c in cats],
        fontsize=CAT_FS, color=INK, rotation=CAT_ROTATION, ha="right", va="center", rotation_mode="anchor",
    )
    ax.tick_params(axis="y", pad=6)
    left_in = max(text_size_in(fig, t)[0] for t in ax.get_yticklabels()) + 6 / 72 + LEFT_PAD_IN

    # agent runs and their pill labels (wrapped when a run is too narrow for its name)
    axes_w_in = FIG_W - left_in - MARGIN_IN
    col_in = axes_w_in / n_att
    runs, xi = [], 0
    for agent, group in groupby(rows, key=lambda r: r["agent"]):
        k = len(list(group))
        runs.append((xi, k, wrap_to_width(fig, ax, agent, (k - 1 + BAR_W) * col_in - 0.12)))
        xi += k
    header_h = HEADER_H_TWO if any("\n" in label for _, _, label in runs) else HEADER_H_ONE

    # vertical layout in rows: categories 0..n-1, the task-id chips above, then the pill row
    # (rows grow downward, so "above" is more negative)
    header_y = TASK_Y - CHIP_H / 2 - HEADER_GAP - header_h / 2
    y_top = header_y - header_h / 2 - 0.15
    y_bottom = len(cats) - 0.5
    axes_h_in = (y_bottom - y_top) * ROW_IN
    fig.set_size_inches(FIG_W, axes_h_in + 2 * MARGIN_IN)
    ax.set_position([left_in / FIG_W, MARGIN_IN / fig.get_figheight(),
                     axes_w_in / FIG_W, axes_h_in / fig.get_figheight()])
    ax.set_ylim(y_bottom, y_top)

    # geometry below reads pixel scales off the axes, so it comes after the limits are final
    for yi, cat in enumerate(cats):
        for xi, row in enumerate(rows):
            draw_cell(ax, xi, yi, cell_counts(row, cat))
    for xi, row in enumerate(rows):  # task id in a small chip
        ax.add_patch(pill(ax, xi - CHIP_W / 2, TASK_Y - CHIP_H / 2, CHIP_W, CHIP_H,
                          facecolor=CHIP_FILL, edgecolor="none", zorder=1, clip_on=False))
        ax.text(xi, TASK_Y, str(row["task_id"]), ha="center", va="center", fontsize=TASK_FS, color=INK, zorder=2)
    for start, k, label in runs:  # agent pill over its run of columns
        x0, x1 = start - BAR_W / 2, start + k - 1 + BAR_W / 2
        ax.add_patch(pill(ax, x0, header_y - header_h / 2, x1 - x0, header_h,
                          facecolor=HEADER_FILL, edgecolor="none", zorder=1, clip_on=False))
        ax.text((x0 + x1) / 2, header_y, label, ha="center", va="center", fontsize=HEADER_FS,
                color=INK, linespacing=1.1, zorder=2)
    # name the two header rows at the left, in line with the chips and pills
    for y, name in ((header_y, "Agent"), (TASK_Y, "Task ID")):
        ax.annotate(name, xy=(-0.5, y), xytext=(-6, 0), textcoords="offset points",
                    ha="right", va="center", fontsize=ROW_LABEL_FS, color=MUTED, annotation_clip=False)

    # bare 2x2 legend (TP TN / FP FN) centred in the empty block left of the axes and
    # above the first category row (the tilted row-0 label rises about half a row)
    block_bottom = ax.transData.transform((0, -1.0))[1] / (fig.get_figheight() * fig.dpi)
    cx = left_in / 2 / FIG_W
    cy = (1.0 + block_bottom) / 2 - LEGEND_DROP
    handles = [Patch(facecolor=COLORS[c], edgecolor="none", label=c) for c in LEGEND_ORDER]
    fig.legend(handles=handles, loc="center", bbox_to_anchor=(cx, cy), ncol=2, frameon=False,
               fontsize=LEGEND_FS_PT, labelcolor=INK, handlelength=1.0, handleheight=0.75,
               columnspacing=0.5, handletextpad=0.35, labelspacing=0.2, borderpad=0.1, borderaxespad=0.0,
               handler_map={Patch: _RoundedHandler()})
    return fig


def report(rows: list[dict]) -> None:
    total = Counter()
    for r in rows:
        total.update(v for v in r["labels"].values() if v in COLORS)
    n = sum(total.values())
    print(f"{len(rows)} annotated attempts, {n} graded check labels; accuracy {(total['TN'] + total['TP']) / n:.1%}")
    print(f"{'category':<36}{'n':>5}{'acc':>7}{'TP':>4}{'FP':>4}{'TN':>4}{'FN':>4}")
    for cat in CATEGORY_ORDER:
        c = Counter()
        for r in rows:
            c.update(cell_counts(r, cat))
        k = sum(c.values())
        if k:
            print(f"{cat:<36}{k:>5}{(c['TN'] + c['TP']) / k:>7.0%}{c['TP']:>4}{c['FP']:>4}{c['TN']:>4}{c['FN']:>4}")


def main():
    rows = fetch()
    if not rows:
        sys.exit("no annotations")
    report(rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig = make_figure(rows)
    for ext in ("png", "pdf"):
        path = OUT_DIR / f"{PLOT_NAME}.{ext}"
        fig.savefig(path, dpi=SAVE_DPI, bbox_inches="tight", facecolor=SURFACE)
        print("wrote", path)
    plt.close(fig)


if __name__ == "__main__":
    main()
