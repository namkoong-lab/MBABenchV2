#!/usr/bin/env python
"""Leaderboard of every graded v2 agent cohort, one column each, best at the left.

Reads the live v2 database (config database.v2_url): for each (agent, task) the
newest clean grading by the current judge (judge_version >= MIN_JUDGE_VERSION,
not failed, not deprecated, attempt and task not deprecated) and its
scored_results.total_score, the 0-100 composite over the twelve rubric_9
categories. A cohort's column is the mean over its graded tasks, with a
standard-error whisker; a cohort graded on fewer than --min-tasks tasks is
left out. The figure widens with the number of cohorts.

Draws leaderboard.{png,pdf} under operation/results/v2/plots/leaderboard/:

  column   the model vendor's hue (style_guide.yaml `company`), squircle cap,
           the score on the cap and "n/N" in muted ink when the cohort is not
           fully graded
  harness  rides on the column itself, two ways at once: a hatch drawn in the
           surface colour (GUI flat, Excel diagonal, Code crossed, In-house
           dotted) and a tint of the vendor hue toward the surface
           (style_guide.yaml `harness_tint`, GUI fullest)
  legend   vendor swatches at the left of the top row, harness swatches at the
           right, drawn on a neutral face so the hatch reads as a pattern

--group instead splits the columns into harness sections, each sorted on its own.

Cohorts are named in AGENTS below; an agent_model_name in the database that is
not there is skipped and listed on stderr, so a new cohort has to be registered
here before it appears. Run with the shared plotting environment,
~/.uv/uv_venvs/base.

Usage:
    python operation/v2/paper_scripts/plot_leaderboard.py
    python operation/v2/paper_scripts/plot_leaderboard.py --group
    python operation/v2/paper_scripts/plot_leaderboard.py --min-tasks 80 --plot-dir /tmp/plots
    python operation/v2/paper_scripts/plot_leaderboard.py --database-url postgresql://...
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import psycopg2
import psycopg2.extras
from matplotlib.legend_handler import HandlerPatch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_difficulty_and_types import (  # noqa: E402
    BAR_ROUND_PT,
    BASELINE,
    GRID,
    INK,
    LEGEND_HANDLE_ROUND_PT,
    MUTED,
    REPO_ROOT,
    STYLE,
    SURFACE,
    UNKNOWN_COLOR,
    _squircle_end_path,
    database_url,
)

PLOT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "leaderboard"
PLOT_NAME = "leaderboard"
MIN_JUDGE_VERSION = 12  # the current judge; earlier versions graded a different check set
MIN_TASKS_DEFAULT = 30  # cohorts graded on fewer tasks are still in flight

COMPANY_COLORS: dict[str, str] = STYLE["company"]
HARNESS_TINT: dict[str, float] = STYLE["harness_tint"]
SLATE = STYLE["ink"]["slate"]

# task_attempts.agent_model_type -> harness name. Fixed order for --group and the legend.
HARNESS = {"gui": "GUI", "excel": "Excel", "coding_cli": "Code", "api": "In-house"}
HARNESS_ORDER = ["GUI", "Excel", "Code", "In-house"]
# Hatch per harness, drawn in the surface colour over the tinted fill. GUI is flat
# and full-strength; each step away from it adds pattern and loses saturation.
HARNESS_HATCH = {"GUI": "", "Excel": "/", "Code": "x", "In-house": "."}
HATCH_LW = 1.0  # points; matplotlib has one hatch line width per figure
HATCH_ALPHA = 0.6  # the hatch is the surface colour at this opacity, so it stays quiet
LEGEND_HARNESS_FACE = SLATE  # neutral face so the legend hatch reads as a pattern

# agent_model_name -> (display name, vendor of the model). Only these are plotted.
AGENTS: dict[str, tuple[str, str]] = {
    # --- GUI: the vendor's chat product driving a desktop ---------------------
    "claude_fable_5_1_cowork_max": ("Claude Cowork (Fable 5.1)", "Anthropic"),
    "claude_fable_5_cowork_max": ("Claude Cowork (Fable 5)", "Anthropic"),
    "claude_opus_5_cowork_max": ("Claude Cowork (Opus 5)", "Anthropic"),
    "chatgpt_gpt_6_astra_work_ultra": ("ChatGPT Work (GPT-6 Astra)", "OpenAI"),
    "chatgpt_gpt_5_6_sol_work_ultra": ("ChatGPT Work (GPT-5.6 Sol)", "OpenAI"),
    "chatgpt_gpt_6_pro": ("ChatGPT Pro (GPT-6)", "OpenAI"),
    # --- Excel: the vendor's add-in inside Excel ---------------------------------
    "claude_excel_fable_5_1": ("Claude for Excel (Fable 5.1)", "Anthropic"),
    "claude_excel_fable_5": ("Claude for Excel (Fable 5)", "Anthropic"),
    "claude_excel_opus_5": ("Claude for Excel (Opus 5)", "Anthropic"),
    "chatgpt_excel_gpt_5_6_sol_xhigh": ("ChatGPT for Excel (GPT-5.6 Sol)", "OpenAI"),
    # --- Code: a coding CLI writing the workbook ----------------------------------
    "claudecode_anthropic/claude-fable-5-1-max": ("Claude Code (Fable 5.1)", "Anthropic"),
    "claudecode_anthropic/claude-fable-5-max": ("Claude Code (Fable 5)", "Anthropic"),
    "codex_openai/gpt-6-astra-xhigh": ("Codex (GPT-6 Astra)", "OpenAI"),
    "codex_openai/gpt-5.6-sol-xhigh": ("Codex (GPT-5.6 Sol)", "OpenAI"),
    "codex_tensorblock/gemini-3.8-flash-high": ("Codex (Gemini 3.8 Flash)", "Google"),
    "codex_tensorblock/grok-4.6-xhigh": ("Codex (Grok 4.6)", "xAI"),
    "codex_tensorblock/kimi-k3-max": ("Codex (Kimi K3)", "Moonshot"),
    # --- In-house: our openpyxl agent over the raw API ---------------------------
    "openpyxl_anthropic/claude-fable-5-1-max": ("Fable 5.1", "Anthropic"),
    "openpyxl_anthropic/claude-fable-5-max": ("Fable 5", "Anthropic"),
    "openpyxl_openai/gpt-6-astra-xhigh": ("GPT-6 Astra", "OpenAI"),
    "openpyxl_openai/gpt-5.6-sol-xhigh": ("GPT-5.6 Sol", "OpenAI"),
    "openpyxl_tensorblock/gemini-3.8-flash-high": ("Gemini 3.8 Flash", "Google"),
    "openpyxl_tensorblock/grok-4.6-xhigh": ("Grok 4.6", "xAI"),
    "openpyxl_tensorblock/kimi-k3-max": ("Kimi K3", "Moonshot"),
    "openpyxl_tensorblock/qwen3.8-max-xhigh": ("Qwen 3.8 Max", "Alibaba"),
}

# ============================================================================
# FIGURE STYLE
# ============================================================================
COL_IN = 0.82  # inches per cohort column; the figure widens with the roster
SECTION_GAP = 0.9  # empty columns between harness sections under --group
MIN_AXES_W_IN = 9.0
AXES_H_IN = 5.2
LEFT_IN = 0.9
RIGHT_IN = 0.3
TOP_IN = 1.0  # legend row plus headroom
TOP_IN_GROUP = 1.55  # ... plus the section header row under --group
SECTION_Y = 103.5  # data units; the header row sits just above the axes
BOTTOM_IN = 2.0  # tilted names
SAVE_DPI = 200

NAME_FS = 14
VALUE_FS = 14
COVER_FS = 10.5
TICK_FS = 13
AXIS_FS = 14
LEGEND_FS = 13
SECTION_FS = 15

BAR_W = 0.68  # column pitch is 1
NAME_ROTATION = 30  # degrees
SE_LW = 1.3
SE_CAP_PT = 3.5
VALUE_PAD = 1.2  # score units between the whisker and the value label
YLIM = (0, 100)
YTICKS = (0, 20, 40, 60, 80, 100)
LEGEND_HANDLE_LENGTH = 2.2

plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["hatch.linewidth"] = HATCH_LW

SQL = """
    select distinct on (ta.agent_model_name, ta.task_id)
           ta.agent_model_name, ta.agent_model_type, ta.task_id,
           (g.scored_results->>'total_score')::float as score
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

    @property
    def n(self) -> int:
        return len(self.scores)

    @property
    def mean(self) -> float:
        return float(np.mean(self.scores))

    @property
    def se(self) -> float:
        return float(np.std(self.scores, ddof=1) / np.sqrt(self.n)) if self.n > 1 else 0.0

    @property
    def color(self) -> str:
        """Vendor hue, mixed toward the surface by the harness tint."""
        base = COMPANY_COLORS.get(self.company, UNKNOWN_COLOR)
        t = HARNESS_TINT.get(self.harness, 0.0)
        rgb = (1 - t) * np.array(mcolors.to_rgb(base)) + t * np.array(mcolors.to_rgb(SURFACE))
        return mcolors.to_hex(rgb)


# --- data ----------------------------------------------------------------------


def fetch(conn, min_tasks: int) -> tuple[list[Cohort], int]:
    """Plotted cohorts (best first) and the size of the live task set."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select count(*) as n from tasks where deprecated is not true")
    n_tasks = cur.fetchone()["n"]
    cur.execute(SQL, {"judge": MIN_JUDGE_VERSION})
    rows = cur.fetchall()

    by_key: dict[str, Cohort] = {}
    unknown: dict[str, int] = {}
    for r in rows:
        key = r["agent_model_name"]
        if key not in AGENTS:
            unknown[key] = unknown.get(key, 0) + 1
            continue
        if key not in by_key:
            name, company = AGENTS[key]
            by_key[key] = Cohort(key, name, company, HARNESS.get(r["agent_model_type"], "?"), [])
        by_key[key].scores.append(r["score"])

    for key, n in sorted(unknown.items()):
        print(f"skipping {key}: {n} graded tasks but not in AGENTS", file=sys.stderr)
    for c in by_key.values():
        if c.n < min_tasks:
            print(f"skipping {c.name}: {c.n} < {min_tasks} graded tasks", file=sys.stderr)
        if c.company not in COMPANY_COLORS:
            print(f"{c.name}: vendor {c.company!r} has no colour in style_guide.yaml", file=sys.stderr)
    kept = [c for c in by_key.values() if c.n >= min_tasks]
    return sorted(kept, key=lambda c: -c.mean), n_tasks


# --- figure ----------------------------------------------------------------------


def layout(cohorts: list[Cohort], group: bool) -> tuple[list[tuple[float, Cohort]], list[tuple[str, float, float]]]:
    """Column x per cohort, plus (harness, x_first, x_last) per section under --group."""
    cols: list[tuple[float, Cohort]] = []
    sections: list[tuple[str, float, float]] = []
    x = 0.0
    if group:
        for h in HARNESS_ORDER:
            members = [c for c in cohorts if c.harness == h]
            if not members:
                continue
            x0 = x
            for c in members:
                cols.append((x, c))
                x += 1
            sections.append((h, x0, x - 1))
            x += SECTION_GAP
    else:
        for c in cohorts:
            cols.append((x, c))
            x += 1
    return cols, sections


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

    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        patch = mpatches.FancyBboxPatch(
            (-xdescent, -ydescent), width, height,
            boxstyle=f"round,pad=0,rounding_size={LEGEND_HANDLE_ROUND_PT}",
            facecolor=orig_handle.get_facecolor(), edgecolor=orig_handle.get_edgecolor(),
            hatch=orig_handle.get_hatch(), linewidth=0,
        )
        patch.set_transform(trans)
        return [patch]


def make_figure(cohorts: list[Cohort], n_tasks: int, group: bool) -> plt.Figure:
    cols, sections = layout(cohorts, group)
    x_last = cols[-1][0]
    axes_w_in = max(MIN_AXES_W_IN, (x_last + 1) * COL_IN)
    fig_w = LEFT_IN + axes_w_in + RIGHT_IN
    top_in = TOP_IN_GROUP if group else TOP_IN
    fig_h = top_in + AXES_H_IN + BOTTOM_IN
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=SURFACE)
    ax = fig.add_axes([LEFT_IN / fig_w, BOTTOM_IN / fig_h, axes_w_in / fig_w, AXES_H_IN / fig_h])
    ax.set_facecolor(SURFACE)
    ax.set_xlim(-0.6, x_last + 0.6)
    ax.set_ylim(*YLIM)

    # recessive chrome: baseline only, hairline grid on the value axis
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.set_yticks(YTICKS, [str(t) for t in YTICKS], fontsize=TICK_FS, color=MUTED)
    ax.tick_params(axis="y", length=0, pad=6)
    ax.set_ylabel("Composite score (0-100)", fontsize=AXIS_FS, color=MUTED, labelpad=10)
    ax.set_xticks([x for x, _ in cols], [c.name for _, c in cols], fontsize=NAME_FS, color=INK,
                  rotation=NAME_ROTATION, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="x", length=0, pad=8)

    # columns (limits are final, so the squircle radius is right), whiskers, labels
    hatch_color = mcolors.to_rgba(SURFACE, HATCH_ALPHA)
    for x, c in cols:
        squircle_column(ax, x, c.mean, facecolor=c.color, edgecolor=hatch_color, linewidth=0,
                        hatch=HARNESS_HATCH.get(c.harness, ""), zorder=2)
        if c.se > 0:
            ax.errorbar(x, c.mean, yerr=c.se, fmt="none", ecolor=INK, elinewidth=SE_LW,
                        capsize=SE_CAP_PT, capthick=SE_LW, zorder=4)
        t = ax.text(x, c.mean + c.se + VALUE_PAD, f"{c.mean:.1f}", ha="center", va="bottom",
                    fontsize=VALUE_FS, color=INK, zorder=4)
        if c.n < n_tasks:
            ax.annotate(f"{c.n}/{n_tasks}", xy=(0.5, 1), xycoords=t, xytext=(0, 2),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=COVER_FS, color=SLATE)

    # section headers under --group: a rule over each section just above the axes
    for h, x0, x1 in sections:
        ax.text((x0 + x1) / 2, SECTION_Y + 1.5, h, ha="center", va="bottom",
                fontsize=SECTION_FS, color=INK, weight="bold", clip_on=False)
        ax.plot([x0 - BAR_W / 2, x1 + BAR_W / 2], [SECTION_Y] * 2, color=BASELINE, lw=1.2,
                solid_capstyle="butt", clip_on=False, zorder=1)

    # top row: vendor legend at the left, harness legend at the right
    vendors = []
    for c in cohorts:
        if c.company not in vendors:
            vendors.append(c.company)
    v_handles = [mpatches.Patch(facecolor=COMPANY_COLORS.get(v, UNKNOWN_COLOR), edgecolor="none", label=v)
                 for v in vendors]
    harnesses = [h for h in HARNESS_ORDER if any(c.harness == h for c in cohorts)]
    h_handles = [mpatches.Patch(facecolor=_tinted(LEGEND_HARNESS_FACE, h), edgecolor=hatch_color,
                                hatch=HARNESS_HATCH[h], label=h) for h in harnesses]
    legend_y = (BOTTOM_IN + AXES_H_IN + top_in - 0.45) / fig_h
    common = dict(frameon=False, fontsize=LEGEND_FS, labelcolor=INK, handlelength=LEGEND_HANDLE_LENGTH,
                  handleheight=0.9, columnspacing=1.4, handletextpad=0.5,
                  handler_map={mpatches.Patch: _HatchedHandler()})
    fig.legend(handles=v_handles, loc="center left", ncol=len(v_handles), title="Model vendor",
               title_fontsize=LEGEND_FS, bbox_to_anchor=(LEFT_IN / fig_w, legend_y), **common)
    fig.legend(handles=h_handles, loc="center right", ncol=len(h_handles), title="Harness",
               title_fontsize=LEGEND_FS, bbox_to_anchor=((LEFT_IN + axes_w_in) / fig_w, legend_y), **common)
    return fig


def _tinted(hex_color: str, harness: str) -> str:
    """The harness tint applied to an arbitrary face colour (legend swatches)."""
    t = HARNESS_TINT.get(harness, 0.0)
    rgb = (1 - t) * np.array(mcolors.to_rgb(hex_color)) + t * np.array(mcolors.to_rgb(SURFACE))
    return mcolors.to_hex(rgb)


def save_figure(fig: plt.Figure, plot_dir: Path, name: str) -> list[Path]:
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "pdf"):
        path = plot_dir / f"{name}.{ext}"
        fig.savefig(path, dpi=SAVE_DPI, bbox_inches="tight", facecolor=SURFACE)
        paths.append(path)
    return paths


def report(cohorts: list[Cohort], n_tasks: int) -> None:
    print(f"{len(cohorts)} cohorts, {n_tasks} live tasks, judge >= v{MIN_JUDGE_VERSION}")
    print(f"{'#':>3}  {'cohort':<34}{'harness':<10}{'vendor':<11}{'n':>4}{'mean':>7}{'se':>6}")
    for i, c in enumerate(cohorts, 1):
        print(f"{i:>3}  {c.name:<34}{c.harness:<10}{c.company:<11}{c.n:>4}{c.mean:>7.1f}{c.se:>6.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", help="full connection string; bypasses config/config.yaml")
    parser.add_argument("--min-tasks", type=int, default=MIN_TASKS_DEFAULT,
                        help=f"drop cohorts graded on fewer tasks (default {MIN_TASKS_DEFAULT})")
    parser.add_argument("--group", action="store_true",
                        help="split the columns into harness sections instead of one ranking")
    parser.add_argument("--plot-dir", type=Path, default=PLOT_DIR,
                        help=f"directory the figure is saved to as {PLOT_NAME}.png/.pdf (default: {PLOT_DIR})")
    args = parser.parse_args()

    conn = psycopg2.connect(database_url(args))
    try:
        cohorts, n_tasks = fetch(conn, args.min_tasks)
    finally:
        conn.close()
    if not cohorts:
        sys.exit("no cohort passes --min-tasks")
    report(cohorts, n_tasks)

    fig = make_figure(cohorts, n_tasks, args.group)
    name = f"{PLOT_NAME}_by_harness" if args.group else PLOT_NAME
    for path in save_figure(fig, args.plot_dir, name):
        print(f"wrote {path}")
    plt.close(fig)


if __name__ == "__main__":
    main()
