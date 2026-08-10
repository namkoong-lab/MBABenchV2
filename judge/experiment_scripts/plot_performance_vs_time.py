"""Combined 2x2 figure: performance and attempt time vs task difficulty.

Top row    = performance (score 0–100)
Bottom row = attempt time (minutes, log y by default)
Left col   = API models
Right col  = GUI models

Shares the difficulty x-axis within each column. Independent y-axes per
panel — score and time live on very different scales, and the two model
groups have very different magnitudes within each metric.

Reads:
  - results/raw_grade_data.json     (per-attempt scores)
  - results/processing_times.json   (per-attempt time_taken_min)

Each row uses its own data source — this is not an inner-join plot. A
model's score column and time column may aggregate over slightly
different (model, task) sets depending on what's present in each JSON.

Usage:
    python judge/experiment_scripts/plot_performance_vs_time.py
    python .../plot_performance_vs_time.py --metric accuracy
    python .../plot_performance_vs_time.py --statistic median
    python .../plot_performance_vs_time.py --time-y-scale linear
"""

import argparse
import colorsys
import json
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


# ============================================================================
# OUTPUT
# ============================================================================
_HERE = Path(__file__).resolve().parent
SAVE_DIR = (_HERE / "../../results/plots").resolve()
SAVE_NAME = "performance_and_time_by_difficulty"
SAVE_DPI = 200
GRADE_JSON = (_HERE / "../../results/raw_grade_data.json").resolve()
TIME_JSON = (_HERE / "../../results/processing_times.json").resolve()


# ============================================================================
# MODEL MAP — kept in sync with the rest of the figure family.
# ============================================================================
MODEL_MAP: dict[str, tuple[str, str, str]] = {
    "openpyxl_allenai/olmo-3.1-32b-think@1105": ("OLMo 3.1 32B", "API", "AI2"),
    "openpyxl_moonshotai/kimi-k2.5@1105": ("Kimi K2", "API", "Moonshot"),
    "openpyxl_openai/gpt-5.4@1105": ("GPT-5.4", "API", "OpenAI"),
    "openpyxl_x-ai/grok-4.20@1105": ("Grok 4.2", "API", "xAI"),
    "openpyxl_google/gemini-3.1-pro-preview@1105": ("Gemini 3.1 Pro", "API", "Google"),
    "openpyxl_anthropic/claude-opus-4-6@1105": ("Opus 4.6", "API", "Anthropic"),
    "chatgpt_web_pro@9": ("ChatGPT Pro", "GUI", "OpenAI"),
    "chatgpt_agent@9": ("ChatGPT Agent", "GUI", "OpenAI"),
    "chatgpt_excel_agent@8": ("ChatGPT (Excel)", "GUI", "OpenAI"),
    "claude_web@9": ("Claude Web", "GUI", "Anthropic"),
    "claude_excel_agent@8": ("Claude (Excel)", "GUI", "Anthropic"),
}

GROUP_ORDER = ["API", "GUI"]


# ============================================================================
# DIFFICULTY AXIS — note the two sources use different keys for the same
# bucket: raw_grade_data.json -> "human_difficulty",
# processing_times.json     -> "human_difficulty_measure".
# A single CLI choice (--difficulty-field) maps to the right key per source.
# ============================================================================
DIFFICULTY_ORDER: dict[str, list[str]] = {
    "human_difficulty": [
        "VERY-EASY",
        "EASY",
        "MEDIUM",
        "MEDIUM-HARD",
        "HARD",
        "VERY-HARD",
    ],
    "fmwc_difficulty": ["EASY", "MEDIUM", "HARD"],
}
DIFFICULTY_DISPLAY: dict[str, dict[str, str]] = {
    "human_difficulty": {
        "VERY-EASY": "1",
        "EASY": "2",
        "MEDIUM": "3",
        "MEDIUM-HARD": "4",
        "HARD": "5",
        "VERY-HARD": "6",
    },
    "fmwc_difficulty": {"EASY": "1", "MEDIUM": "2", "HARD": "3"},
}
EXCLUDED_BUCKETS: dict[str, list[str]] = {
    "human_difficulty": ["VERY-HARD"],
    "fmwc_difficulty": [],
}

# raw_grade_data uses "human_difficulty"; processing_times uses
# "human_difficulty_measure". Same bucket strings — only the column name
# differs.
DIFFICULTY_FIELD_IN_TIME_JSON: dict[str, str] = {
    "human_difficulty": "human_difficulty_measure",
    "fmwc_difficulty": "fmwc_difficulty",
}

METRIC_CHOICES = ["total", "accuracy", "formula", "format"]
METRIC_DISPLAY = {
    "total": "total score",
    "accuracy": "accuracy score",
    "formula": "formula score",
    "format": "format score",
}


# ============================================================================
# STYLE — mirrors the rest of the figure family.
# ============================================================================
EMBED_FONTS_IN_PDF = True
AXES_LINEWIDTH = 0.6

plt.rcParams.update(
    {
        "font.family": "Helvetica",
        "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
        "text.usetex": False,
        "axes.unicode_minus": False,
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

TICK_FONTSIZE = 22
LABEL_FONTSIZE = 24
DIFFICULTY_TICK_FS = 22

FIG_SIZE = (18, 11.5)

LINE_WIDTH = 2.4
MARKER_SIZE = 7
MARKER_EDGE_WIDTH = 1.4
MARKER_EDGE_COLOR = "white"
ERR_CAPSIZE = 0
ERR_LINEWIDTH = 1.4
LINE_ALPHA = 0.95

FAMILY_LIGHTNESS_SPREAD = 0.34
FAMILY_DARK_THRESHOLD = 0.15
FAMILY_DARK_UP_RANGE = 0.50

LEGEND_FONTSIZE = 20
LEGEND_FRAMEON = True
LEGEND_FACECOLOR = "white"
LEGEND_EDGECOLOR = "none"
LEGEND_FRAMEALPHA = 0.35
LEGEND_BORDERPAD = 0.5
LEGEND_LABELSPACING = 0.35
LEGEND_HANDLELENGTH = 1.5
LEGEND_HANDLETEXTPAD = 0.5
LEGEND_HANDLE_LINEWIDTH = 6.0
# Legends only on the top (performance) row — same models repeat in the
# bottom row with the same colors, so a second legend is redundant.
# Performance lines fall as difficulty rises, so upper-right is the
# emptiest corner in both top panels.
LEGEND_LOC_PERF: dict[str, str] = {"API": "upper right", "GUI": "upper right"}
LEGEND_BBOX_PERF: dict[str, tuple[float, float] | None] = {
    "API": None,
    "GUI": None,
}
LEGEND_NCOL_PERF: dict[str, int] = {"API": 1, "GUI": 1}


# ============================================================================
# DATA LOAD
# ============================================================================
def load_grade_rows(
    path: Path, metric: str, difficulty_field: str
) -> list[dict]:
    """Per-attempt score rows from raw_grade_data.json."""
    with open(path) as f:
        payload = json.load(f)
    out: list[dict] = []
    for r in payload["rows"]:
        if r.get("model_label") not in MODEL_MAP:
            continue
        diff = r.get(difficulty_field)
        v = r.get(metric)
        if diff is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        out.append(
            {"model_label": r["model_label"], "difficulty": diff, "value": v}
        )
    return out


def load_time_rows(path: Path, difficulty_field_grade: str) -> list[dict]:
    """Per-attempt time rows from processing_times.json. Translates the
    grade-side difficulty key to whatever processing_times stores it as.
    """
    with open(path) as f:
        payload = json.load(f)

    rows = payload["rows"]
    # processing_times.json stores difficulty on a single key called
    # "difficulty" (the value of "difficulty_field" in its config tells
    # which underlying column). Confirm the snapshot matches what the
    # caller asked for.
    cfg = payload.get("config", {})
    expected = DIFFICULTY_FIELD_IN_TIME_JSON.get(difficulty_field_grade)
    snapshot_field = cfg.get("difficulty_field")
    if expected and snapshot_field and snapshot_field != expected:
        print(
            f"[plot] WARNING: time JSON difficulty_field is {snapshot_field!r}, "
            f"requested {expected!r}. Plot may show mismatched buckets."
        )

    out: list[dict] = []
    for r in rows:
        if r.get("model_label") not in MODEL_MAP:
            continue
        diff = r.get("difficulty")
        v = r.get("time_taken_min")
        if diff is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        out.append(
            {"model_label": r["model_label"], "difficulty": diff, "value": v}
        )
    return out


# ============================================================================
# AGGREGATION
# ============================================================================
def aggregate(rows: list[dict], difficulties: list[str]) -> dict[str, dict[str, dict]]:
    """{model_label: {difficulty: {mean, sem, median, q25, q75, n}}}."""
    by_md: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        if r["difficulty"] not in difficulties:
            continue
        by_md[(r["model_label"], r["difficulty"])].append(r["value"])

    result: dict[str, dict[str, dict]] = defaultdict(dict)
    for (label, diff), vals in by_md.items():
        arr = np.asarray(vals, dtype=float)
        n = arr.size
        mean = float(arr.mean())
        sem = float(arr.std(ddof=1) / np.sqrt(n)) if n > 1 else 0.0
        median = float(np.median(arr))
        q25 = float(np.quantile(arr, 0.25))
        q75 = float(np.quantile(arr, 0.75))
        result[label][diff] = {
            "mean": mean,
            "sem": sem,
            "median": median,
            "q25": q25,
            "q75": q75,
            "n": n,
        }
    return result


# ============================================================================
# COLOR HELPERS — copied from the rest of the family.
# ============================================================================
def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        max(0, min(255, int(round(r * 255)))),
        max(0, min(255, int(round(g * 255)))),
        max(0, min(255, int(round(b * 255)))),
    )


def vary_within_family(hex_color: str, idx: int, total: int) -> str:
    if total <= 1:
        return hex_color
    r, g, b = _hex_to_rgb(hex_color)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if l < FAMILY_DARK_THRESHOLD:
        new_l = l + (idx / (total - 1)) * FAMILY_DARK_UP_RANGE
    else:
        new_l = (
            l
            - FAMILY_LIGHTNESS_SPREAD / 2
            + (idx / (total - 1)) * FAMILY_LIGHTNESS_SPREAD
        )
    new_l = max(0.05, min(0.92, new_l))
    nr, ng, nb = colorsys.hls_to_rgb(h, new_l, s)
    return _rgb_to_hex(nr, ng, nb)


PANEL_COLOR_OVERRIDES: dict[str, str] = {
    "chatgpt_web_pro@9": "#2B2B2B",
    "chatgpt_agent@9": "#10A37F",
    "chatgpt_excel_agent@8": "#1F6F73",
}


def assign_panel_colors(panel_labels: list[str]) -> dict[str, str]:
    family_members: dict[str, list[str]] = defaultdict(list)
    for lbl in panel_labels:
        family = MODEL_MAP[lbl][2]
        family_members[family].append(lbl)
    colors: dict[str, str] = {}
    for family, members in family_members.items():
        base = MODEL_FAMILY_COLORS.get(family, "#666666")
        non_override = [m for m in members if m not in PANEL_COLOR_OVERRIDES]
        for i, lbl in enumerate(non_override):
            colors[lbl] = vary_within_family(base, i, len(non_override))
        for lbl in members:
            if lbl in PANEL_COLOR_OVERRIDES:
                colors[lbl] = PANEL_COLOR_OVERRIDES[lbl]
    return colors


# ============================================================================
# PLOT
# ============================================================================
def order_labels(present_labels: set[str]) -> list[str]:
    def key(lbl: str):
        display, group, family = MODEL_MAP[lbl]
        gi = GROUP_ORDER.index(group) if group in GROUP_ORDER else 99
        return (gi, family, display)

    return sorted([lbl for lbl in MODEL_MAP if lbl in present_labels], key=key)


def split_by_group(ordered_labels: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {g: [] for g in GROUP_ORDER}
    for lbl in ordered_labels:
        out.setdefault(MODEL_MAP[lbl][1], []).append(lbl)
    return out


def _center_and_err(cell: dict, statistic: str) -> tuple[float, float, float]:
    if statistic == "mean":
        c, s = cell["mean"], cell["sem"]
        return c, s, s
    if statistic == "median":
        c = cell["median"]
        return c, max(c - cell["q25"], 0.0), max(cell["q75"] - c, 0.0)
    raise ValueError(f"unknown statistic {statistic!r}")


def perf_y_bounds(agg, panel_labels, difficulties, statistic):
    uppers = []
    for lbl in panel_labels:
        for d in difficulties:
            cell = agg[lbl].get(d)
            if not cell or cell["n"] == 0:
                continue
            c, _, ehi = _center_and_err(cell, statistic)
            uppers.append(c + ehi)
    if not uppers:
        return (0.0, 100.0)
    return (0.0, min(100.0, max(uppers) * 1.10))


def time_y_bounds(agg, panel_labels, difficulties, statistic, log_scale):
    centers, uppers = [], []
    for lbl in panel_labels:
        for d in difficulties:
            cell = agg[lbl].get(d)
            if not cell or cell["n"] == 0:
                continue
            c, _, ehi = _center_and_err(cell, statistic)
            centers.append(c)
            uppers.append(c + ehi)
    if not centers:
        return (0.1, 1.0) if log_scale else (0.0, 1.0)
    top = max(uppers) * (1.20 if log_scale else 1.10)
    if log_scale:
        positive = [c for c in centers if c > 0]
        bot = (min(positive) / 2.0) if positive else 0.1
        return (bot, top)
    return (0.0, top)


def draw_line_panel(
    ax,
    agg: dict[str, dict[str, dict]],
    panel_labels: list[str],
    difficulties: list[str],
    difficulty_field: str,
    panel_colors: dict[str, str],
    statistic: str,
    show_xticklabels: bool,
    legend: tuple[str, tuple[float, float] | None, int] | None,
) -> None:
    if not panel_labels or not difficulties:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return

    x_full = np.arange(len(difficulties))

    handles, labels = [], []
    for lbl in panel_labels:
        display = MODEL_MAP[lbl][0]
        color = panel_colors[lbl]

        xs, centers, err_lo, err_hi = [], [], [], []
        for xi, d in zip(x_full, difficulties):
            cell = agg[lbl].get(d)
            if not cell or cell["n"] == 0:
                continue
            c, lo, hi = _center_and_err(cell, statistic)
            xs.append(xi)
            centers.append(c)
            err_lo.append(lo)
            err_hi.append(hi)

        if not xs:
            continue

        (line_artist,) = ax.plot(
            xs, centers, color=color, linewidth=LINE_WIDTH,
            alpha=LINE_ALPHA, zorder=2,
        )
        ax.errorbar(
            xs, centers, yerr=[err_lo, err_hi], fmt="none",
            ecolor=color, elinewidth=ERR_LINEWIDTH, capsize=ERR_CAPSIZE,
            alpha=0.85, zorder=2,
        )
        ax.plot(
            xs, centers, marker="o", markersize=MARKER_SIZE,
            markerfacecolor=color, markeredgecolor=MARKER_EDGE_COLOR,
            markeredgewidth=MARKER_EDGE_WIDTH, linestyle="none", zorder=3,
        )
        handles.append(line_artist)
        labels.append(display)

    ax.set_xticks(x_full)
    if show_xticklabels:
        ax.set_xticklabels(
            [
                DIFFICULTY_DISPLAY[difficulty_field].get(d, str(i + 1))
                for i, d in enumerate(difficulties)
            ],
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

    if legend is not None and handles:
        loc, bbox, ncol = legend
        leg = ax.legend(
            handles, labels, loc=loc, bbox_to_anchor=bbox,
            fontsize=LEGEND_FONTSIZE, frameon=LEGEND_FRAMEON,
            facecolor=LEGEND_FACECOLOR, edgecolor=LEGEND_EDGECOLOR,
            framealpha=LEGEND_FRAMEALPHA, borderpad=LEGEND_BORDERPAD,
            labelspacing=LEGEND_LABELSPACING, handlelength=LEGEND_HANDLELENGTH,
            handletextpad=LEGEND_HANDLETEXTPAD, ncol=ncol,
        )
        for line in leg.get_lines():
            line.set_linewidth(LEGEND_HANDLE_LINEWIDTH)


# ============================================================================
# DRIVER
# ============================================================================
def do_plot(args) -> None:
    grade_path = Path(args.grade_json) if args.grade_json else GRADE_JSON
    time_path = Path(args.time_json) if args.time_json else TIME_JSON
    for p in (grade_path, time_path):
        if not p.exists():
            raise SystemExit(f"[plot] missing input JSON: {p}")

    grade_rows = load_grade_rows(grade_path, args.metric, args.difficulty_field)
    time_rows = load_time_rows(time_path, args.difficulty_field)
    if not grade_rows or not time_rows:
        raise SystemExit("[plot] empty score or time rows after filtering.")

    canonical = DIFFICULTY_ORDER[args.difficulty_field]
    if args.include_very_hard:
        excluded: set[str] = set()
    else:
        excluded = set(EXCLUDED_BUCKETS[args.difficulty_field])
    present_diffs = {r["difficulty"] for r in grade_rows} | {
        r["difficulty"] for r in time_rows
    }
    difficulties = [d for d in canonical if d in present_diffs and d not in excluded]

    present = {r["model_label"] for r in grade_rows} | {
        r["model_label"] for r in time_rows
    }
    missing = [lbl for lbl in MODEL_MAP if lbl not in present]
    if missing:
        print("[plot] no data for: " + ", ".join(MODEL_MAP[m][0] for m in missing))

    ordered_labels = order_labels(present)
    by_group = split_by_group(ordered_labels)

    perf_agg = aggregate(grade_rows, difficulties)
    time_agg = aggregate(time_rows, difficulties)

    log_time = args.time_y_scale == "log"

    fig, axes = plt.subplots(
        2,
        len(GROUP_ORDER),
        figsize=FIG_SIZE,
        sharex="col",
        sharey=False,
    )

    # Top row: performance. Bottom row: time.
    for col_idx, group in enumerate(GROUP_ORDER):
        panel_labels = by_group.get(group, [])
        panel_colors = assign_panel_colors(panel_labels)

        ax_perf = axes[0, col_idx]
        ax_time = axes[1, col_idx]

        # Top: performance
        draw_line_panel(
            ax_perf,
            perf_agg,
            panel_labels,
            difficulties,
            args.difficulty_field,
            panel_colors,
            statistic=args.statistic,
            show_xticklabels=False,
            legend=(
                LEGEND_LOC_PERF.get(group, "upper right"),
                LEGEND_BBOX_PERF.get(group),
                LEGEND_NCOL_PERF.get(group, 1),
            ),
        )
        bot, top = perf_y_bounds(
            perf_agg, panel_labels, difficulties, statistic=args.statistic
        )
        ax_perf.set_ylim(bot, top)

        # Bottom: time
        draw_line_panel(
            ax_time,
            time_agg,
            panel_labels,
            difficulties,
            args.difficulty_field,
            panel_colors,
            statistic=args.statistic,
            show_xticklabels=True,
            legend=None,
        )
        bot, top = time_y_bounds(
            time_agg,
            panel_labels,
            difficulties,
            statistic=args.statistic,
            log_scale=log_time,
        )
        if log_time:
            ax_time.set_yscale("log")
        ax_time.set_ylim(bot, top)

    stat_word = "Median" if args.statistic == "median" else "Mean"
    axes[0, 0].set_ylabel(
        f"{stat_word} {METRIC_DISPLAY[args.metric]}",
        fontsize=LABEL_FONTSIZE,
        labelpad=8,
    )
    axes[1, 0].set_ylabel(
        f"{stat_word} attempt time (minutes)",
        fontsize=LABEL_FONTSIZE,
        labelpad=8,
    )

    fig.tight_layout(rect=(0, 0.03, 1, 1), w_pad=2.0, h_pad=1.6)
    fig.supxlabel("Task difficulty", fontsize=LABEL_FONTSIZE, y=0.00)

    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{args.metric}_{args.statistic}_time-{args.time_y_scale}"
    out_png = SAVE_DIR / f"{SAVE_NAME}{suffix}.png"
    out_pdf = SAVE_DIR / f"{SAVE_NAME}{suffix}.pdf"
    fig.savefig(out_png, dpi=SAVE_DPI, bbox_inches="tight", pad_inches=0.15)
    fig.savefig(out_pdf, bbox_inches="tight", pad_inches=0.15)

    bold_cyan = "\033[1;96m"
    reset = "\033[0m"
    bar = "=" * 72
    print(f"\n{bold_cyan}{bar}{reset}")
    print(f"{bold_cyan}Saved plot to:{reset}")
    print(f"  {bold_cyan}{out_png}{reset}")
    print(f"  {bold_cyan}{out_pdf}{reset}")
    print(f"{bold_cyan}{bar}{reset}\n")


# ============================================================================
# MAIN
# ============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Combined 2x2 figure: performance and attempt time vs task "
            "difficulty, with API/GUI as columns."
        )
    )
    parser.add_argument(
        "--metric",
        choices=METRIC_CHOICES,
        default="total",
        help="Score column. Default: total.",
    )
    parser.add_argument(
        "--statistic",
        choices=["mean", "median"],
        default="mean",
        help="Center statistic. Default: mean.",
    )
    parser.add_argument(
        "--difficulty-field",
        choices=list(DIFFICULTY_ORDER.keys()),
        default="human_difficulty",
    )
    parser.add_argument(
        "--include-very-hard",
        action="store_true",
        help="Include excluded buckets (e.g. VERY-HARD for human difficulty).",
    )
    parser.add_argument(
        "--time-y-scale",
        choices=["log", "linear"],
        default="log",
        help="Time-row y scale. Default: log (times span ~2 orders of magnitude).",
    )
    parser.add_argument("--grade-json", default=None)
    parser.add_argument("--time-json", default=None)

    args = parser.parse_args()
    do_plot(args)


if __name__ == "__main__":
    main()
