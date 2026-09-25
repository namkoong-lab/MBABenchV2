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
    ~/.uv/uv_venvs/base/bin/python operation/v2/paper_scripts/plot_rubric_map.py
    ~/.uv/uv_venvs/base/bin/python operation/v2/paper_scripts/plot_rubric_map.py --font Avenir --out /tmp/map.png
"""

import argparse
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

# Typeface name -> (cell-text face, section-title face); a face is (file, collection index) or
# (file, index, variation name) for a variable font. Titles take a heavier face.
_SYS, _SUP, _USR = "/System/Library/Fonts", "/System/Library/Fonts/Supplemental", str(Path.home() / "Library/Fonts")
FONTS = {
    "Gill Sans": ((f"{_SUP}/GillSans.ttc", 0), (f"{_SUP}/GillSans.ttc", 4)),            # Regular / SemiBold
    "Avenir": ((f"{_SYS}/Avenir.ttc", 0), (f"{_SYS}/Avenir.ttc", 4)),                   # Book / Heavy
    "Avenir Next": ((f"{_SYS}/Avenir Next.ttc", 7), (f"{_SYS}/Avenir Next.ttc", 2)),    # Regular / Demi Bold
    "Helvetica": ((f"{_SYS}/HelveticaNeue.ttc", 0), (f"{_SYS}/HelveticaNeue.ttc", 10)), # Regular / Medium
    "Seravek": ((f"{_SUP}/Seravek.ttc", 0), (f"{_SUP}/Seravek.ttc", 3)),                # Regular / Medium
    "Optima": ((f"{_SYS}/Optima.ttc", 0), (f"{_SYS}/Optima.ttc", 1)),                   # Regular / Bold
    "Futura": ((f"{_SUP}/Futura.ttc", 0), (f"{_SUP}/Futura.ttc", 2)),                   # Medium / Bold
    "Inter": ((f"{_USR}/Inter-Regular.otf", 0), (f"{_USR}/Inter-SemiBold.otf", 0)),
    # serif
    "Charter": ((f"{_SUP}/Charter.ttc", 0), (f"{_SUP}/Charter.ttc", 3)),                # Roman / Bold
    "Iowan": ((f"{_SUP}/Iowan Old Style.ttc", 0), (f"{_SUP}/Iowan Old Style.ttc", 1)),  # Roman / Bold
    "STIX Two": ((f"{_SUP}/STIXTwoText.ttf", 0), (f"{_SUP}/STIXTwoText.ttf", 0, "SemiBold")),
    "Times": ((f"{_SYS}/Times.ttc", 0), (f"{_SYS}/Times.ttc", 1)),                      # Regular / Bold
    "Palatino": ((f"{_SYS}/Palatino.ttc", 0), (f"{_SYS}/Palatino.ttc", 2)),             # Regular / Bold
    "Georgia": ((f"{_SUP}/Georgia.ttf", 0), (f"{_SUP}/Georgia Bold.ttf", 0)),
}
FONT = STYLE["font"]

INK = STYLE["ink"]["primary"]
SURFACE = STYLE["ink"]["surface"]

# Category hues, assigned by weight rank: the style guide's eight categorical
# slots first, then four extras chosen to sit between them.
HUES = list(STYLE["categorical"]) + ["#0e8a9e", "#9a6b3a", "#5f6f8f", "#9b4f96"]

# ---- layout knobs ------------------------------------------------------------
# Same page footprint as the other v2 figures: 18 x 8 in at 200 dpi. Sizes below
# are in points (1/72 in) and converted to pixels through PT.
DPI = 200
SUPERSAMPLE = 2              # draw at this multiple of DPI and downsample, so rounded corners are antialiased
FIG_IN = (18, 8)
PT = DPI * SUPERSAMPLE / 72
W, H = int(FIG_IN[0] * DPI * SUPERSAMPLE), int(FIG_IN[1] * DPI * SUPERSAMPLE)
REGION_RADIUS = 8 * PT       # corner radius of each category region; item cells stay square-cornered
TOP = 0                      # no title: the map is the whole figure
GAP = int(4 * PT)            # surface-coloured gap between category regions
# At \textwidth the 18 in canvas prints at 5.5/18 = 0.31x: 36 pt -> 11 pt, 24 pt -> 7.3 pt.
LABEL_FS = 36                # category name, pt
LABEL_ONE_LINE_FS = 30       # name shrinks to here to keep the tail beside it, then the tail wraps
LABEL_SHORTEN_FS = 16        # a name that overflows even at this size drops its ' & ...' part
LABEL_MIN_FS = 14            # a name wider than its region shrinks down to this
LABEL_TAIL_FS = 24           # the '19% · 3 items' tail
LABEL_TAIL_MIN_FS = 16       # the tail shrinks down to this to stay beside the name
LABEL_TAIL_FLOOR_FS = 10     # a wrapped tail shrinks down to this to show whole (Rounding's narrow region)
LABEL_SEP = 0.4              # gap between name and tail, in tail ems
LABEL_TAIL_RATIO = 0.8       # a wrapped tail is at most this share of its name's size
LABEL_PAD = 4 * PT           # band padding above the name and below the label
INSET = int(3 * PT)          # cells inset from the region edge
BORDER = max(1, int(0.8 * PT))   # cell border width
CELL_PAD = int(2.5 * PT)     # text inset from the cell border
MAX_FONT = int(64 * PT)      # cap for cell text
MIN_FONT = int(4 * PT)       # floor for cell text
REGION_TINT = 0.16           # hue share in the region background (label band)
CELL_TINT = 0.07             # hue share in the cell fill
TEXT_DARKEN = 0.30           # ink share mixed into the hue for cell text
LABEL_DARKEN = 0.45          # ink share mixed into the hue for the section label (name and tail)


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

_fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def pil_font(size, title=False):
    """The typeface at `size` px: regular face for cell text, the heavier face for section titles."""
    size = int(round(size))
    if (size, title) not in _fonts:
        path, index, *variation = FONTS[FONT][1 if title else 0]
        font = ImageFont.truetype(path, size, index=index)
        if variation:
            font.set_variation_by_name(variation[0])
        _fonts[size, title] = font
    return _fonts[size, title]


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


def format_share(share):
    """'13%', or '6.5%' when the share is not a whole percent at one decimal."""
    pct = round(share * 100, 1)
    return f"{pct:.0f}%" if pct == round(pct) else f"{pct:.1f}%"


def draw_label(d, x, y, w, cat, share, n, hue):
    """'Category' (title face) then 'share · n items', both in the darkened hue; returns the band height.

    Name and tail share one line where they fit, both shrinking (name LABEL_FS down to LABEL_ONE_LINE_FS,
    tail LABEL_TAIL_FS down to LABEL_TAIL_MIN_FS) by the same fraction of their full size, so neither
    gives up much alone. A narrower region puts the tail on a second line, shrunk (to LABEL_TAIL_FLOOR_FS
    at most) to fit whole; only a name wider than the region shrinks further.
    """
    limit = w - 10 * PT
    colour = mix(INK, hue, LABEL_DARKEN)
    tail = f"{format_share(share)} · {n} item{'s' if n != 1 else ''}"
    name_w = lambda s: d.textlength(cat, font=pil_font(s * PT, title=True))
    tail_w = lambda t: d.textlength(tail, font=pil_font(t * PT))
    if name_w(LABEL_SHORTEN_FS) > limit:  # 'Model Outputs & Executive Summary' -> 'Model Outputs'
        cat = cat.split(" &")[0]
    pairs = [(s, t) for s in range(LABEL_FS, LABEL_ONE_LINE_FS - 1, -1)
             for t in range(LABEL_TAIL_FS, LABEL_TAIL_MIN_FS - 1, -1)
             if name_w(s) + t * PT * LABEL_SEP + tail_w(t) <= limit]
    # least total relative shrink; ties keep the larger name
    shrink = lambda p: (round((1 - p[0] / LABEL_FS) + (1 - p[1] / LABEL_TAIL_FS), 3), -p[0])
    size, tsize = min(pairs, key=shrink) if pairs else (None, None)
    one_line = size is not None
    if not one_line:
        size = next((s for s in range(LABEL_FS, LABEL_MIN_FS, -1) if name_w(s) <= limit), LABEL_MIN_FS)
        top = min(LABEL_TAIL_FS, round(size * LABEL_TAIL_RATIO))  # never outsize a shrunken name
        tsize = next((t for t in range(top, LABEL_TAIL_FLOOR_FS - 1, -1) if tail_w(t) <= limit),
                     LABEL_TAIL_FLOOR_FS)
    ft = pil_font(tsize * PT)
    base = y + LABEL_PAD + pil_font(LABEL_FS * PT, title=True).getmetrics()[0] * 0.8  # shared first baseline
    d.text((x, base), cat, font=pil_font(size * PT, title=True), fill=colour, anchor="ls")
    if one_line:
        d.text((x + name_w(size) + tsize * PT * LABEL_SEP, base), tail, font=ft, fill=colour, anchor="ls")
    else:
        base += tsize * PT * 1.1
        d.text((x, base), tail, font=ft, fill=colour, anchor="ls")
    return base - y + LABEL_PAD * 1.5


def main():
    global FONT
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font", default=FONT, choices=sorted(FONTS), help="typeface (default: style_guide.yaml font)")
    ap.add_argument("--out", type=Path, default=OUT_PATH, help="output .png; a .pdf is written beside it")
    args = ap.parse_args()
    FONT = args.font
    out_path = args.out

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
        d.rounded_rectangle([x0, y0, x1, y1], radius=REGION_RADIUS, fill=mix(hue, SURFACE, REGION_TINT))
        band = draw_label(d, x0 + 5 * PT, y0, x1 - x0, cat, share, len(its), hue)
        ix, iy = x0 + INSET, y0 + band
        iw, ih = (x1 - x0) - 2 * INSET, (y1 - y0) - band - INSET
        ordered = sorted(its, key=lambda i: -i["weight"])
        cells = layout([i["weight"] for i in ordered], ix, iy, iw, ih)
        for it, (cx, cy, cw, ch) in zip(ordered, cells):
            box = (round(cx), round(cy), round(cx + cw), round(cy + ch))
            if not draw_cell(d, box, it, hue):
                unfitted.append((cat, it["name"], f"{int(cw)}x{int(ch)}"))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = canvas.resize((W // SUPERSAMPLE, H // SUPERSAMPLE), Image.LANCZOS)
    canvas.save(out_path, dpi=(DPI, DPI))
    canvas.save(out_path.with_suffix(".pdf"), resolution=DPI)
    print("wrote", out_path, "and", out_path.with_suffix(".pdf").name)
    for cat, name, size in unfitted:
        print(f"  WARNING: '{name}' ({cat}) did not fit its {size} px cell at {MIN_FONT} px")


if __name__ == "__main__":
    main()
