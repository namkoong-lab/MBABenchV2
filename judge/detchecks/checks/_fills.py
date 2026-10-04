"""Helpers shared by checks 47 and 94 (group "fills"): what a conditional-format fill paints.

Measured by Patrick in Excel (2026-10-03, detchecks/out/excel_session_answers.md Q2): for a
conditional-format (dxf) SOLID fill Excel paints bgColor ONLY.  fgColor is ignored; bgColor
indexed 64 and bgColor rgb 00000000 paint BLACK (the alpha byte is ignored); no bgColor -> no
fill.  So ChatGPT's tool (fgColor=X + bgColor indexed 64) paints black, openpyxl's
PatternFill(start_color=X, fill_type="solid") (bgColor 00000000) paints black, and a dxf with
fgColor only paints nothing.  The core implements this (styles.fill_paint(..., dxf=True));
there is no ambiguity left, so nothing here raises.
"""
from __future__ import annotations


def dxf_fill_paints(styles, fill) -> tuple[list[str], bool]:
    """(colours 'FFRRGGBB', together) for a conditional-format (dxf) fill.
    together=True: every colour is painted (gradient stops).  Otherwise one colour (the paint)
    or none.  ([], False) when the dxf paints no fill."""
    if fill is None or fill.kind == "none":
        return [], False
    p = styles.fill_paint(fill, dxf=True)
    if p.kind == "none":
        return [], False
    if fill.kind == "gradient":
        return list(p.stops), True
    return ([p.effective] if p.effective else []), False
