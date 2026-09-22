#!/usr/bin/env python
"""Rubric map: a two-level treemap of the 129 rubric items (operation/v2/rubrics.csv).

Level 1: one region per category, area = the category's share of the total
score. Level 2: inside each region one bordered cell per item, area = that
item's share of the total score, with the item's name fitted into the cell.
Each category has its own hue: a light tint fills the region and its cells,
the cell borders and text use the hue itself.

Colours (the categorical slots) and the typeface come from
operation/v2/style_guide.yaml; four extra hues cover categories 9-12.
Output: operation/results/v2/plots/rubric_map/rubric_map.{png,pdf}, 18 x 8 in at
200 dpi like the other v2 figures (the PDF is the same raster, sized in inches).

Run with the shared plotting environment:
    ~/.uv/uv_venvs/base/bin/python operation/v2/paper_scripts/rubric_map.py
"""

import csv
from collections import defaultdict
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

# This file lives at <repo>/operation/v2/paper_scripts/, so the repo root is four levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]
STYLE = yaml.safe_load((REPO_ROOT / "operation" / "v2" / "style_guide.yaml").read_text())
CSV_PATH = REPO_ROOT / "operation" / "v2" / "rubrics.csv"
OUT_PATH = REPO_ROOT / "operation" / "results" / "v2" / "plots" / "rubric_map" / "rubric_map.png"
FONT_PATH = "/System/Library/Fonts/Supplemental/GillSans.ttc"  # index 0 = Regular

INK = STYLE["ink"]["primary"]
MUTED = STYLE["ink"]["muted"]
SURFACE = STYLE["ink"]["surface"]

# Category hues, assigned by weight rank: the style guide's eight categorical
# slots first, then four extras chosen to sit between them.
HUES = list(STYLE["categorical"]) + ["#0e8a9e", "#9a6b3a", "#5f6f8f", "#9b4f96"]

# ---- layout knobs ------------------------------------------------------------
# Same page footprint as the other v2 figures: 18 x 8 in at 200 dpi. Sizes below
# are in points (1/72 in) and converted to pixels through PT.
DPI = 200
FIG_IN = (18, 8)
PT = DPI / 72
W, H = int(FIG_IN[0] * DPI), int(FIG_IN[1] * DPI)
TOP = 0                      # no title: the map is the whole figure
GAP = int(4 * PT)            # surface-coloured gap between category regions
LABEL_FS = 26                # category name, pt (tick text in the sibling figures is 16 pt)
LABEL_TAIL_FS = 17           # the '19% · 3 items' tail
LABEL_H = int(38 * PT)       # label band inside each region
INSET = int(3 * PT)          # cells inset from the region edge
BORDER = max(1, int(0.8 * PT))   # cell border width
CELL_PAD = int(2.5 * PT)     # text inset from the cell border
MAX_FONT = int(64 * PT)      # cap for cell text
MIN_FONT = int(4 * PT)       # floor for cell text
SCALE = 1                    # kept for draw_label's small-size table
REGION_TINT = 0.16           # hue share in the region background (label band)
CELL_TINT = 0.07             # hue share in the cell fill
TEXT_DARKEN = 0.30           # ink share mixed into the hue for cell text


# --- data --------------------------------------------------------------------


def load_items() -> list[dict]:
    rows = list(csv.reader(CSV_PATH.open(newline="")))
    items = []
    for r in rows[1:]:
        if not r[0].strip().isdigit():
            continue
        items.append(
            {
                "no": int(r[0]),
                "category": r[1].strip(),
                "name": r[2].strip(),
                "weight": float(r[6].strip().rstrip("%")) / 100,
            }
        )
    return items


def categories(items):
    by = defaultdict(list)
    for it in items:
        by[it["category"]].append(it)
    return sorted(by.items(), key=lambda kv: -sum(i["weight"] for i in kv[1]))


# --- colour ------------------------------------------------------------------


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """t of colour a over (1 - t) of colour b, both hex; returns an RGB tuple."""
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return tuple(round(x * t + y * (1 - t)) for x, y in zip(ra, rb))


# --- treemap -----------------------------------------------------------------


def squarify(values, x, y, w, h):
    """Squarified treemap (Bruls, Huizing, van Wijk). `values` descending, sum == w*h."""
    rects = []
    values = list(values)
    while values:
        vertical = w >= h           # wide box: lay the next row as a column on the left
        side = h if vertical else w
        row, rest, best = [], values[:], None
        while rest:
            cand = row + [rest[0]]
            thick = sum(cand) / side
            worst = max(max(thick * thick / v, v / (thick * thick)) for v in cand)
            if best is not None and worst > best:
                break
            best, row, rest = worst, cand, rest[1:]
        thick = sum(row) / side
        off = 0
        for v in row:
            length = v / thick
            rects.append((x, y + off, thick, length) if vertical else (x + off, y, length, thick))
            off += length
        if vertical:
            x, w = x + thick, w - thick
        else:
            y, h = y + thick, h - thick
        values = rest
    return rects


def layout(values, x, y, w, h):
    """squarify with values rescaled to the box area; values kept in given order."""
    total = sum(values)
    return squarify([v / total * w * h for v in values], x, y, w, h)


# --- text fitting ------------------------------------------------------------

_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def pil_font(size):
    size = int(round(size))
    if size not in _fonts:
        _fonts[size] = ImageFont.truetype(FONT_PATH, size, index=0)
    return _fonts[size]


def wrap(draw, text, font, width):
    """Greedy word wrap; returns lines, or None if a single word is wider than `width`."""
    lines, line = [], ""
    for word in text.split():
        cand = f"{line} {word}".strip()
        if draw.textlength(cand, font=font) <= width:
            line = cand
        else:
            if not line:
                return None
            lines.append(line)
            line = word
            if draw.textlength(word, font=font) > width:
                return None
    lines.append(line)
    return lines


def char_wrap(draw, text, font, width):
    """Last resort: break inside words with a hyphen so the text fits `width`."""
    lines, line = [], ""
    for ch in text:
        cand = line + ch
        if draw.textlength(cand + "-", font=font) <= width or ch == " " or not line:
            line = cand
        else:
            lines.append(line.rstrip() + "-")
            line = ch
    lines.append(line.strip())
    return lines


def fit_text(draw, text, width, height):
    """Largest font (stepping down by 1 px) whose wrapped text fits width x height."""
    for size in range(MAX_FONT, MIN_FONT - 1, -1):
        font = pil_font(size)
        lines = wrap(draw, text, font, width)
        if lines is None:
            continue
        line_h = size * 1.08
        if len(lines) * line_h <= height:
            return font, lines, line_h
    for size in range(MIN_FONT, int(MIN_FONT * 0.7), -1):  # slivers: hyphenate, a hair smaller
        font = pil_font(size)
        lines = char_wrap(draw, text, font, width)
        line_h = size * 1.08
        if len(lines) * line_h <= height:
            return font, lines, line_h
    return None, None, None


# --- drawing -----------------------------------------------------------------


def draw_cell(d, box, it, hue):
    x0, y0, x1, y1 = box
    d.rectangle([x0, y0, x1, y1], fill=mix(hue, SURFACE, CELL_TINT), outline=hex_to_rgb(hue), width=BORDER)
    iw, ih = (x1 - x0) - 2 * (BORDER + CELL_PAD), (y1 - y0) - 2 * (BORDER + CELL_PAD)
    if iw <= 0 or ih <= 0:
        return False
    font, lines, line_h = fit_text(d, it["name"], iw, ih)
    if font is None:
        return False
    total_h = len(lines) * line_h
    cy = y0 + BORDER + CELL_PAD + (ih - total_h) / 2
    colour = mix(INK, hue, TEXT_DARKEN)
    for i, line in enumerate(lines):
        lw = d.textlength(line, font=font)
        cx = x0 + BORDER + CELL_PAD + (iw - lw) / 2
        d.text((cx, cy + i * line_h), line, font=font, fill=colour)
    return True


def draw_label(d, x, y, w, cat, share, n, hue):
    """'Category' in the hue, then ' share · n items' in muted, shrinking to fit."""
    tail = f"  {share:.0%} · {n} item{'s' if n != 1 else ''}"
    limit = w - 8 * PT
    for size in range(LABEL_FS, 13, -1):
        f, fs = pil_font(size * PT), pil_font(min(LABEL_TAIL_FS, size - 6) * PT)
        if d.textlength(cat, font=f) + d.textlength(tail, font=fs) <= limit:
            break
    else:  # narrow region: drop the tail, then shorten the name
        tail = f"  {share:.0%}"
        if d.textlength(cat, font=f) + d.textlength(tail, font=fs) > limit:
            cat = cat.split(" &")[0]
    base = y + LABEL_FS * PT * 0.95  # shared baseline whatever size the name ended up
    d.text((x, base), cat, font=f, fill=mix(INK, hue, 0.45), anchor="ls")
    d.text((x + d.textlength(cat, font=f), base), tail, font=fs, fill=MUTED, anchor="ls")


def main():
    items = load_items()
    cats = categories(items)
    weights = [sum(i["weight"] for i in its) for _, its in cats]
    regions = layout(weights, 0, TOP, W, H - TOP)

    canvas = Image.new("RGB", (W, H), SURFACE)
    d = ImageDraw.Draw(canvas)

    unfitted = []
    for k, ((cat, its), (x, y, w, h), share) in enumerate(zip(cats, regions, weights)):
        hue = HUES[k]
        x0, y0 = int(x + GAP / 2), int(y + GAP / 2)
        x1, y1 = int(x + w - GAP / 2), int(y + h - GAP / 2)
        d.rectangle([x0, y0, x1, y1], fill=mix(hue, SURFACE, REGION_TINT))
        draw_label(d, x0 + 5 * PT, y0 + 4 * PT, x1 - x0, cat, share, len(its), hue)
        ix, iy = x0 + INSET, y0 + LABEL_H
        iw, ih = (x1 - x0) - 2 * INSET, (y1 - y0) - LABEL_H - INSET
        ordered = sorted(its, key=lambda i: -i["weight"])
        cells = layout([i["weight"] for i in ordered], ix, iy, iw, ih)
        for it, (cx, cy, cw, ch) in zip(ordered, cells):
            box = (round(cx), round(cy), round(cx + cw), round(cy + ch))
            if not draw_cell(d, box, it, hue):
                unfitted.append((cat, it["name"], f"{int(cw)}x{int(ch)}"))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUT_PATH, dpi=(DPI, DPI))
    canvas.save(OUT_PATH.with_suffix(".pdf"), resolution=DPI)
    print("wrote", OUT_PATH, "and", OUT_PATH.with_suffix(".pdf").name)
    for cat, name, size in unfitted:
        print(f"  WARNING: '{name}' ({cat}) did not fit its {size} px cell at {MIN_FONT} px")


if __name__ == "__main__":
    main()
