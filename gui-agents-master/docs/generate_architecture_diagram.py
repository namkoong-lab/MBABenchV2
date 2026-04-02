#!/usr/bin/env python3
"""Generate the unified Playwright AI Agent Architecture diagram.

Produces both PNG and SVG in the same directory.
"""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from pathlib import Path

# ── Palette ──────────────────────────────────────────────────────────────────
GREEN_FILL, GREEN_EDGE = "#D5EDDA", "#2E7D32"
BLUE_FILL, BLUE_EDGE = "#D0E4F7", "#1565C0"
GRAY_FILL, GRAY_EDGE = "#E8E8E8", "#616161"
ORANGE_BG, ORANGE_EDGE = "#FFF3E0", "#E65100"
PURPLE_BG, PURPLE_EDGE = "#F3E5F5", "#7B1FA2"
DARK = "#1A1A1A"
MID = "#555555"


def rounded_box(
    ax,
    x,
    y,
    w,
    h,
    label,
    sub=None,
    fill=BLUE_FILL,
    edge=BLUE_EDGE,
    fs=12,
    sub_fs=9.5,
    bold=True,
    radius=0.006,
):
    """Draw a rounded box with optional subtitle."""
    box = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0.006,rounding_size={radius}",
        facecolor=fill,
        edgecolor=edge,
        linewidth=2.4,
        zorder=3,
    )
    ax.add_patch(box)
    cx, cy = x + w / 2, y + h / 2
    if sub:
        ax.text(
            cx,
            cy + h * 0.17,
            label,
            ha="center",
            va="center",
            fontsize=fs,
            fontweight="bold" if bold else "normal",
            color=DARK,
            zorder=4,
        )
        ax.text(
            cx,
            cy - h * 0.20,
            sub,
            ha="center",
            va="center",
            fontsize=sub_fs,
            color=MID,
            fontstyle="italic",
            zorder=4,
        )
    else:
        ax.text(
            cx,
            cy,
            label,
            ha="center",
            va="center",
            fontsize=fs,
            fontweight="bold" if bold else "normal",
            color=DARK,
            zorder=4,
        )


def layer_band(ax, y, h, label, bg_color="#F5F5F5", label_color="#999999"):
    """Full-width layer band. Label is placed ABOVE the band so boxes never cover it."""
    band = FancyBboxPatch(
        (0.03, y),
        0.94,
        h,
        boxstyle="round,pad=0.004,rounding_size=0.006",
        facecolor=bg_color,
        edgecolor="none",
        linewidth=0,
        zorder=1,
    )
    ax.add_patch(band)
    ax.text(
        0.042,
        y + h + 0.005,
        label,
        ha="left",
        va="bottom",
        fontsize=9.5,
        fontweight="bold",
        color=label_color,
        fontstyle="italic",
        zorder=5,
    )


def arrow(ax, x1, y1, x2, y2, color="#90A4AE", lw=1.6, dashed=False):
    ax.annotate(
        "",
        xy=(x2, y2),
        xytext=(x1, y1),
        arrowprops=dict(
            arrowstyle="-|>",
            color=color,
            lw=lw,
            linestyle="dashed" if dashed else "solid",
        ),
        zorder=2,
    )


def build_diagram():
    fig, ax = plt.subplots(figsize=(14, 19))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("white")

    # ── Title ────────────────────────────────────────────────────────────
    ax.text(
        0.50,
        0.98,
        "Playwright AI Agent — Unified Architecture",
        ha="center",
        va="top",
        fontsize=21,
        fontweight="bold",
        color=DARK,
    )
    ax.text(
        0.50,
        0.957,
        "Composable framework for browser-automated AI research workflows",
        ha="center",
        va="top",
        fontsize=11,
        color="#888888",
        fontstyle="italic",
    )

    # ════════════════════════════════════════════════════════════════════
    #  LAYER 1 — INPUT
    # ════════════════════════════════════════════════════════════════════
    L1_y, L1_h = 0.840, 0.070
    layer_band(
        ax, L1_y, L1_h, "INPUT LAYER  ·  User-Configurable", "#E8F5E9", GREEN_EDGE
    )

    bw, bh = 0.25, 0.045
    by = L1_y + (L1_h - bh) / 2
    rounded_box(
        ax,
        0.055,
        by,
        bw,
        bh,
        "Task Configs",
        "tasks_configs/*.yaml",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=12,
    )
    rounded_box(
        ax,
        0.375,
        by,
        bw,
        bh,
        "Prompt Templates",
        "templates/{agent}.yaml",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=12,
    )
    rounded_box(
        ax,
        0.695,
        by,
        bw,
        bh,
        "Agent Parameters",
        "model · timeout · retry",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=12,
    )

    # ════════════════════════════════════════════════════════════════════
    #  LAYER 2 — ORCHESTRATION
    # ════════════════════════════════════════════════════════════════════
    L2_y, L2_h = 0.732, 0.070
    layer_band(
        ax, L2_y, L2_h, "ORCHESTRATION LAYER  ·  Framework", "#E3F2FD", BLUE_EDGE
    )

    ow, oh = 0.52, bh
    ox = 0.24
    oy = L2_y + (L2_h - oh) / 2
    rounded_box(
        ax,
        ox,
        oy,
        ow,
        oh,
        "Batch Automation Runner",
        "Retry logic · subprocess isolation · dual-counter recovery",
        fill=BLUE_FILL,
        edge=BLUE_EDGE,
        fs=13,
    )

    orch_cx = ox + ow / 2
    for bx in [0.055 + bw / 2, 0.375 + bw / 2, 0.695 + bw / 2]:
        arrow(ax, bx, by, orch_cx, oy + oh, color=GREEN_EDGE, lw=1.4)

    # ════════════════════════════════════════════════════════════════════
    #  LAYER 3 — ENGINE
    # ════════════════════════════════════════════════════════════════════
    L3_y, L3_h = 0.615, 0.078
    layer_band(ax, L3_y, L3_h, "ENGINE LAYER  ·  Framework", "#E3F2FD", BLUE_EDGE)

    ew, eh = 0.46, 0.050
    ex = 0.05
    ey = L3_y + (L3_h - eh) / 2
    rounded_box(
        ax,
        ex,
        ey,
        ew,
        eh,
        "Engine (Pipeline)",
        "Setup → Navigate → Launch Agent → Send Prompts → Download",
        fill=BLUE_FILL,
        edge=BLUE_EDGE,
        fs=12.5,
    )

    bmx, bmw = 0.56, 0.38
    rounded_box(
        ax,
        bmx,
        ey,
        bmw,
        eh,
        "Browser Manager",
        "Playwright · Chrome CDP · Firefox persistent",
        fill=GRAY_FILL,
        edge=GRAY_EDGE,
        fs=12,
    )

    arrow(ax, orch_cx, oy, ex + ew / 2, ey + eh, color=BLUE_EDGE, lw=1.4)
    arrow(ax, ex + ew, ey + eh / 2, bmx, ey + eh / 2, color=GRAY_EDGE, lw=1.4)

    # ════════════════════════════════════════════════════════════════════
    #  LAYER 4 — NAVIGATION
    # ════════════════════════════════════════════════════════════════════
    L4_y, L4_h = 0.490, 0.085
    layer_band(ax, L4_y, L4_h, "NAVIGATION LAYER  ·  Mixed", ORANGE_BG, ORANGE_EDGE)

    nw, nh = 0.33, 0.055
    ny = L4_y + (L4_h - nh) / 2
    rounded_box(
        ax,
        0.055,
        ny,
        nw,
        nh,
        "OneDrive Navigation",
        "Folder-by-folder · fuzzy match · scroll-scan",
        fill=BLUE_FILL,
        edge=BLUE_EDGE,
        fs=12,
    )

    ax.text(
        0.50,
        ny + nh / 2,
        "OR",
        ha="center",
        va="center",
        fontsize=19,
        fontweight="bold",
        color=ORANGE_EDGE,
        zorder=5,
    )

    rounded_box(
        ax,
        0.605,
        ny,
        nw,
        nh,
        "Direct URL / Local Path",
        "User provides link · skip OneDrive entirely",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=12,
    )

    engine_cx = ex + ew / 2
    arrow(ax, engine_cx - 0.06, ey, 0.055 + nw / 2, ny + nh, color=BLUE_EDGE, lw=1.4)
    arrow(ax, engine_cx + 0.06, ey, 0.605 + nw / 2, ny + nh, color=GREEN_EDGE, lw=1.4)

    # ════════════════════════════════════════════════════════════════════
    #  LAYER 5 — AI INTERACTION
    # ════════════════════════════════════════════════════════════════════
    L5_y, L5_h = 0.355, 0.095
    layer_band(ax, L5_y, L5_h, "AI INTERACTION LAYER", "#E3F2FD", BLUE_EDGE)

    aw, ah = 0.20, 0.058
    ay = L5_y + (L5_h - ah) / 2

    a1x, a2x, a3x = 0.04, 0.275, 0.51
    rounded_box(
        ax,
        a1x,
        ay,
        aw,
        ah,
        "Claude",
        "claude.ai · Excel Add-in",
        fill=BLUE_FILL,
        edge=BLUE_EDGE,
        fs=12,
    )

    # OR between Claude and ChatGPT
    ax.text(
        (a1x + aw + a2x) / 2,
        ay + ah / 2,
        "OR",
        ha="center",
        va="center",
        fontsize=11,
        fontweight="bold",
        color=BLUE_EDGE,
        zorder=5,
    )

    rounded_box(
        ax,
        a2x,
        ay,
        aw,
        ah,
        "ChatGPT",
        "chatgpt.com · Excel Add-in",
        fill=BLUE_FILL,
        edge=BLUE_EDGE,
        fs=12,
    )

    # OR between ChatGPT and Your Agent
    ax.text(
        (a2x + aw + a3x) / 2,
        ay + ah / 2,
        "OR",
        ha="center",
        va="center",
        fontsize=11,
        fontweight="bold",
        color=BLUE_EDGE,
        zorder=5,
    )

    rounded_box(
        ax,
        a3x,
        ay,
        aw,
        ah,
        "Your Agent",
        "Extend base class",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=12,
    )

    ms_x, ms_w = 0.745, 0.21
    rounded_box(
        ax,
        ms_x,
        ay,
        ms_w,
        ah,
        "Model Selection",
        "Configurable per agent",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=11.5,
    )

    # nav → AI
    ai_cx = 0.42
    arrow(ax, 0.055 + nw / 2, ny, ai_cx - 0.04, ay + ah, color=BLUE_EDGE, lw=1.4)
    arrow(ax, 0.605 + nw / 2, ny, ai_cx + 0.04, ay + ah, color=GREEN_EDGE, lw=1.4)

    # model selection → agents (dashed)
    for agent_right in [a1x + aw, a2x + aw, a3x + aw]:
        arrow(
            ax,
            ms_x,
            ay + ah * 0.65,
            agent_right,
            ay + ah * 0.85,
            color=GREEN_EDGE,
            lw=1.0,
            dashed=True,
        )

    # ════════════════════════════════════════════════════════════════════
    #  LAYER 6 — OUTPUT
    # ════════════════════════════════════════════════════════════════════
    L6_y, L6_h = 0.230, 0.085
    layer_band(
        ax, L6_y, L6_h, "OUTPUT LAYER  ·  User-Configurable", "#E8F5E9", GREEN_EDGE
    )

    ow2, oh2 = 0.27, 0.052
    oy2 = L6_y + (L6_h - oh2) / 2

    rounded_box(
        ax,
        0.04,
        oy2,
        ow2,
        oh2,
        "Excel / File Output",
        "Downloaded .xlsx · local save mode",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=11.5,
    )
    rounded_box(
        ax,
        0.365,
        oy2,
        ow2,
        oh2,
        "Validation",
        "Customizable checks · sheet schema",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=11.5,
    )
    rounded_box(
        ax,
        0.69,
        oy2,
        ow2,
        oh2,
        "Logs & Statistics",
        "JSON timing · status · batch summary",
        fill=GRAY_FILL,
        edge=GRAY_EDGE,
        fs=11.5,
    )

    arrow(ax, ai_cx - 0.10, ay, 0.04 + ow2 / 2, oy2 + oh2, color=BLUE_EDGE, lw=1.4)
    arrow(ax, ai_cx, ay, 0.365 + ow2 / 2, oy2 + oh2, color=BLUE_EDGE, lw=1.4)
    arrow(ax, ai_cx + 0.10, ay, 0.69 + ow2 / 2, oy2 + oh2, color=GRAY_EDGE, lw=1.4)

    # ════════════════════════════════════════════════════════════════════
    #  LEGEND
    # ════════════════════════════════════════════════════════════════════
    leg_y = 0.215
    rounded_box(
        ax,
        0.04,
        leg_y,
        0.12,
        0.024,
        "Composable",
        fill=GREEN_FILL,
        edge=GREEN_EDGE,
        fs=9.5,
        bold=False,
    )
    ax.text(
        0.170,
        leg_y + 0.012,
        "= edit to adapt",
        ha="left",
        va="center",
        fontsize=9.5,
        color=MID,
    )
    rounded_box(
        ax,
        0.345,
        leg_y,
        0.12,
        0.024,
        "Framework",
        fill=BLUE_FILL,
        edge=BLUE_EDGE,
        fs=9.5,
        bold=False,
    )
    ax.text(
        0.475,
        leg_y + 0.012,
        "= stable internals",
        ha="left",
        va="center",
        fontsize=9.5,
        color=MID,
    )
    rounded_box(
        ax,
        0.66,
        leg_y,
        0.14,
        0.024,
        "Infrastructure",
        fill=GRAY_FILL,
        edge=GRAY_EDGE,
        fs=9.5,
        bold=False,
    )
    ax.text(
        0.812,
        leg_y + 0.012,
        "= shared utilities",
        ha="left",
        va="center",
        fontsize=9.5,
        color=MID,
    )

    # ════════════════════════════════════════════════════════════════════
    #  ADAPT FOR YOUR RESEARCH
    # ════════════════════════════════════════════════════════════════════
    panel_y, panel_h = 0.020, 0.170
    layer_band(ax, panel_y, panel_h, "ADAPT FOR YOUR RESEARCH", PURPLE_BG, PURPLE_EDGE)

    steps = [
        (
            "Prompt Templates",
            "Write your own task instructions in templates/{agent}.yaml",
        ),
        ("Task List", "Define your tasks in tasks_configs/ — one YAML per batch"),
        ("Model", "Set model: opus_4_6 / sonnet_4_6 / haiku or GPT variant"),
        ("Navigation", "Use direct_url to skip OneDrive — works with any hosted file"),
        (
            "Output Validation",
            "Customize validation checks in file_organizer.py for your schema",
        ),
        ("New Agent", "Extend the base class and register in the engine factory"),
    ]

    sx = 0.065
    sy = panel_y + panel_h - 0.010
    for i, (title, desc) in enumerate(steps, 1):
        ax.text(
            sx,
            sy,
            f"{i}.",
            ha="left",
            va="top",
            fontsize=11,
            fontweight="bold",
            color=PURPLE_EDGE,
            zorder=5,
        )
        ax.text(
            sx + 0.030,
            sy,
            title,
            ha="left",
            va="top",
            fontsize=11,
            fontweight="bold",
            color=DARK,
            zorder=5,
        )
        ax.text(
            sx + 0.030,
            sy - 0.016,
            desc,
            ha="left",
            va="top",
            fontsize=9.5,
            color=MID,
            zorder=5,
        )
        sy -= 0.028

    return fig


if __name__ == "__main__":
    out_dir = Path(__file__).parent
    fig = build_diagram()
    for fmt in ("png", "svg"):
        path = out_dir / f"architecture_diagram.{fmt}"
        fig.savefig(path, dpi=250, bbox_inches="tight", facecolor="white")
        print(f"Saved {path}")
    plt.close(fig)
