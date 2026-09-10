"""Workbook/sheet properties block (judge v6, item D — "serve the blind evidence").

The 2026-09-01 evidence sweep found ~34 rubric checks graded on properties
the judge is never shown: true tab order (the file listing was sorted
alphabetically), hidden sheets/rows/columns (served as ordinary content),
data validation, column widths and row heights, cell comments, conditional
formats, hyperlinks, defined names, calc mode, print setup. This module
extracts all of that ONCE at CSV-extraction time, saves it beside the CSVs
(`_workbook_properties.json`, so the per-workbook cache carries it and repeat
gradings reuse identical facts), and renders it as a compact, deterministic
text block for the judge's seed prompt.

Where a property cannot be read the block says so explicitly ("unknown"),
so the model can tell "absent" from "not provided".

Cache generation: files written here ride in `*_csv_cache_v6` — a v2 cache
has no properties file and the loaders degrade to the old behaviour
(alphabetical listing, no block), which is why the generation was bumped.
`_v6` (2026-09-10 pm): light yellows at the 60-degree hue boundary are named
`light_yellow`, not `olive` (canary on check 47); schema unchanged.
`_v5` (2026-09-10, judge v7 tier 2): schema 3 adds the active cell per
sheet, a count of styled-but-empty cells inside the used range, the number
of spill/array ranges per sheet, and the resolved hex beside every theme
colour (tab colours, CF styles); hidden defined names leave the rendered
list for the footnote. The cell extractor changed alongside (theme colours
tokenised, `[Red]`/locale number formats rendered, spill anchors and
children tagged, blue-family colour names, `[TEXT]` on numeric-looking
text constants).
`_v4` (2026-09-09, judge v7): schema 2 adds cell hyperlinks (openpyxl fills
`ws._hyperlinks` only when writing), page breaks, row/column grouping,
conditional-format styling, hidden defined names, a zip-based VBA test and
the attempt's original filename; the cell extractor changed alongside
(dates rendered like Excel, accounting padding, blank-format hiding, array /
data-table tagging). A v3 cache lacks all of that, so the generation moved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from openpyxl.utils import get_column_letter

try:
    from .logger import logger
except ImportError:  # imported as a bare module (utils/ on sys.path)
    from logger import logger

FILENAME = "_workbook_properties.json"
SCHEMA_VERSION = 3   # 3 (2026-09-10): active cell, styled empty cells, spill counts, theme hex on colours
                     # 2 (2026-09-09): hyperlinks, page breaks, grouping, CF styles, hidden names, vba, origin
_MAX_LIST = 25          # per-list cap in the rendered text (JSON keeps everything)
_MAX_COMMENT_CHARS = 160


def _safe(fn, default="unknown"):
    try:
        v = fn()
        return default if v is None else v
    except Exception:  # noqa: BLE001 — property reads must never break extraction
        return default


def _runs(pairs: list[tuple[int, Any]]) -> list[dict]:
    """[(index, value)] sorted by index -> [{first, last, value}] runs of
    consecutive indexes with equal values."""
    runs: list[dict] = []
    for idx, val in sorted(pairs, key=lambda p: p[0]):
        if runs and runs[-1]["last"] == idx - 1 and runs[-1]["value"] == val:
            runs[-1]["last"] = idx
        else:
            runs.append({"first": idx, "last": idx, "value": val})
    return runs


def _col_runs_text(runs: list[dict], fmt=lambda v: v) -> str:
    parts = []
    for r in runs:
        a, b = get_column_letter(r["first"]), get_column_letter(r["last"])
        parts.append(f"{a}{'' if a == b else ':' + b}={fmt(r['value'])}")
    return ", ".join(parts)


def _row_runs_text(runs: list[dict], fmt=lambda v: v) -> str:
    parts = []
    for r in runs:
        a, b = r["first"], r["last"]
        parts.append(f"{a}{'' if a == b else '-' + str(b)}={fmt(r['value'])}")
    return ", ".join(parts)


def _ranges_text(indexes: list[int], col: bool) -> str:
    if not indexes:
        return "none"
    runs = _runs([(i, True) for i in indexes])
    parts = []
    for r in runs:
        if col:
            a, b = get_column_letter(r["first"]), get_column_letter(r["last"])
        else:
            a, b = str(r["first"]), str(r["last"])
        parts.append(a if a == b else f"{a}:{b}" if col else f"{a}-{b}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _color_text(color, palette=None) -> Optional[str]:
    """openpyxl Color -> 'rgb:FFC000' / 'theme:4 tint +0.40 (8FAADC)' /
    'indexed:12' / None.

    `.rgb` on a theme colour is a descriptor ERROR STRING in openpyxl
    ("Values must be of type <class 'str'>"), so read the type first. With a
    `palette` (theme_palette.load_palette) the resolved hex is shown beside
    the theme slot.
    """
    if color is None:
        return None
    ctype = getattr(color, "type", None)
    if ctype == "rgb":
        rgb = getattr(color, "rgb", None)
        return f"rgb:{rgb}" if isinstance(rgb, str) and len(rgb) in (6, 8) else None
    if ctype == "theme":
        tint = getattr(color, "tint", 0) or 0
        text = f"theme:{getattr(color, 'theme', '?')}" + (f" tint {tint:+.2f}" if tint else "")
        if palette is not None:
            try:
                from . import theme_palette as _theme
            except ImportError:  # bare-module import path
                import theme_palette as _theme
            hx = _safe(lambda: _theme.resolve_color(palette, color), None)
            if hx:
                text += f" ({hx})"
        return text
    if ctype == "indexed":
        return f"indexed:{getattr(color, 'indexed', '?')}"
    return None


# Defined names that add-ins/system tooling plant in workbooks (Capital IQ,
# @RISK, Palisade, print areas, solver) — counted, not listed.
_SYSTEM_NAME_PREFIXES = ("IQ_", "IQB_", "Risk", "Pal_", "_xlnm", "solver_", "_xlfn", "Slicer_")


def _sheet_properties(ws, index: int, output_name: Optional[str], palette=None) -> dict:
    kind = "chartsheet" if type(ws).__name__ == "Chartsheet" else "worksheet"
    props: dict[str, Any] = {
        "name": ws.title,
        "output_name": output_name,
        "index": index,
        "kind": kind,
        "state": _safe(lambda: ws.sheet_state, "visible"),
        "tab_color": _safe(lambda: _color_text(ws.sheet_properties.tabColor, palette), None),
    }
    if kind == "chartsheet":
        return props

    props.update({
        "max_row": _safe(lambda: ws.max_row, 0),
        "max_column": _safe(lambda: ws.max_column, 0),
        "zoom": _safe(lambda: ws.sheet_view.zoomScale, None),
        "gridlines": _safe(lambda: ws.sheet_view.showGridLines, None),
        "freeze_panes": _safe(lambda: ws.freeze_panes, None),
        "merged_ranges": _safe(lambda: sorted(str(r) for r in ws.merged_cells.ranges), []),
        "protected": _safe(lambda: bool(ws.protection.sheet), None),
        "print_area": _safe(lambda: ws.print_area, None),
        "print_title_rows": _safe(lambda: ws.print_title_rows, None),
        "print_title_cols": _safe(lambda: ws.print_title_cols, None),
        "page_orientation": _safe(lambda: ws.page_setup.orientation, None),
        "fit_to_page": _safe(
            lambda: bool(ws.sheet_properties.pageSetUpPr.fitToPage)
            if ws.sheet_properties.pageSetUpPr else False,
            None,
        ),
    })

    # used range + comments + hyperlinks + counts in one pass over the cells
    min_r = min_c = None
    max_r = max_c = 0
    n_values = n_formulas = n_spills = 0
    comments = []
    links = []
    styled_empty: list[tuple[int, int, str]] = []  # (row, col, coordinate), row-major
    try:
        try:
            from .sheet_extent import iter_rows_kwargs
        except ImportError:  # bare-module import path
            from sheet_extent import iter_rows_kwargs
        _bounds, _ = iter_rows_kwargs(ws)
        for row in ws.iter_rows(**_bounds):
            for cell in row:
                v = cell.value
                if v is None:
                    if _is_styled_empty(cell):
                        styled_empty.append((cell.row, cell.column, cell.coordinate))
                    continue
                r, c = cell.row, cell.column
                min_r = r if min_r is None or r < min_r else min_r
                min_c = c if min_c is None or c < min_c else min_c
                max_r, max_c = max(max_r, r), max(max_c, c)
                n_values += 1
                if isinstance(v, str) and v.startswith("=") and v != "=":
                    # a bare "=" is a spilled child (<f ca="1"/>), not a formula
                    n_formulas += 1
                elif type(v).__name__ == "ArrayFormula":
                    n_formulas += 1
                    ref = str(getattr(v, "ref", "") or "")
                    if ":" in ref and ref.split(":")[0] != ref.split(":")[1]:
                        n_spills += 1
                elif type(v).__name__ == "DataTableFormula":
                    n_formulas += 1
            for cell in row:
                cm = getattr(cell, "comment", None)
                if cm is not None:
                    comments.append({
                        "ref": cell.coordinate,
                        "author": getattr(cm, "author", None),
                        "text": (getattr(cm, "text", "") or "")[:_MAX_COMMENT_CHARS],
                    })
                # Hyperlink objects live on the cell after a load; the
                # sheet-level ws._hyperlinks list is only populated on write.
                hl = getattr(cell, "hyperlink", None)
                if hl is not None:
                    links.append({
                        "ref": cell.coordinate,
                        "target": getattr(hl, "target", None) or getattr(hl, "location", None),
                        "display": getattr(hl, "display", None),
                    })
        props["used_range"] = (
            f"{get_column_letter(min_c)}{min_r}:{get_column_letter(max_c)}{max_r}"
            if min_r is not None else None
        )
        props["n_values"] = n_values
        props["n_formulas"] = n_formulas
        props["n_spill_anchors"] = n_spills
        props["comments"] = comments
        props["hyperlinks"] = sorted(links, key=lambda d: d["ref"])
        # Styled cells with no value INSIDE the used range (rubric_9 check 28,
        # "no unused formatting"): the extractor skips them, so this count is
        # the only place the judge can see leftover formatting sprawl.
        if min_r is not None:
            inside = [
                coord for (r, c, coord) in styled_empty
                if min_r <= r <= max_r and min_c <= c <= max_c
            ]
        else:
            inside = []
        props["styled_empty_cells"] = {"count": len(inside), "examples": inside[:10]}
    except Exception:  # noqa: BLE001
        props["used_range"] = "unknown"
        props["comments"] = "unknown"
        props["hyperlinks"] = "unknown"
        props["styled_empty_cells"] = "unknown"

    # hidden rows / cols, widths / heights, outline grouping
    try:
        hidden_rows, heights, row_groups = [], [], []
        for idx, dim in ws.row_dimensions.items():
            if getattr(dim, "hidden", False):
                hidden_rows.append(int(idx))
            h = getattr(dim, "height", None)
            if h is not None and getattr(dim, "customHeight", None) is not False:
                heights.append((int(idx), round(float(h), 1)))
            lvl = getattr(dim, "outlineLevel", 0) or 0
            if lvl:
                row_groups.append((int(idx), int(lvl)))
        props["hidden_rows"] = sorted(hidden_rows)
        props["row_heights"] = _runs(heights)
        props["row_groups"] = _runs(row_groups)
        props["default_row_height"] = _safe(lambda: ws.sheet_format.defaultRowHeight, None)
    except Exception:  # noqa: BLE001
        props["hidden_rows"] = "unknown"
        props["row_heights"] = "unknown"
        props["row_groups"] = "unknown"
    try:
        hidden_cols, widths, col_groups = [], [], []
        for key, dim in ws.column_dimensions.items():
            lo = int(getattr(dim, "min", None) or 0) or None
            hi = int(getattr(dim, "max", None) or 0) or None
            if lo is None:
                from openpyxl.utils import column_index_from_string
                lo = hi = column_index_from_string(key)
            hi = hi or lo
            if hi - lo > 200:  # openpyxl sometimes reports a 16384-wide default dim
                hi = lo
            if getattr(dim, "hidden", False):
                hidden_cols.extend(range(lo, hi + 1))
            w = getattr(dim, "width", None)
            if w is not None and getattr(dim, "customWidth", True):
                for c in range(lo, hi + 1):
                    widths.append((c, round(float(w), 2)))
            lvl = getattr(dim, "outlineLevel", 0) or 0
            if lvl:
                for c in range(lo, hi + 1):
                    col_groups.append((c, int(lvl)))
        props["hidden_cols"] = sorted(set(hidden_cols))
        props["column_widths"] = _runs(widths)
        props["col_groups"] = _runs(col_groups)
        props["default_col_width"] = _safe(lambda: ws.sheet_format.defaultColWidth, None)
    except Exception:  # noqa: BLE001
        props["hidden_cols"] = "unknown"
        props["column_widths"] = "unknown"
        props["col_groups"] = "unknown"

    # active cell (rubric_9 check 62). openpyxl stores one <selection> per
    # pane; on a frozen-pane sheet the first one belongs to a corner pane, so
    # the cursor is the selection whose pane is the view's activePane.
    try:
        sv = ws.sheet_view
        sels = list(getattr(sv, "selection", None) or [])
        pane = getattr(sv, "pane", None)
        active_pane = getattr(pane, "activePane", None) if pane is not None else None
        chosen = next((sel for sel in sels if getattr(sel, "pane", None) == active_pane), None)
        if chosen is None and sels:
            chosen = sels[-1]
        ac = None
        if chosen is not None:
            ac = getattr(chosen, "activeCell", None)
            if not ac:
                sq = str(getattr(chosen, "sqref", "") or "")
                ac = sq.split()[0].split(":")[0] if sq else None
        # No <selection> element at all is Excel's default: cursor on A1.
        props["active_cell"] = ac or ("A1" if not sels else "unknown")
        tlc = getattr(sv, "topLeftCell", None)
        props["top_left_cell"] = str(tlc) if tlc else None
    except Exception:  # noqa: BLE001
        props["active_cell"] = "unknown"
        props["top_left_cell"] = None

    # page breaks (manual breaks only; automatic ones are not stored)
    try:
        props["row_breaks"] = sorted(int(b.id) for b in ws.row_breaks.brk)
        props["col_breaks"] = sorted(int(b.id) for b in ws.col_breaks.brk)
    except Exception:  # noqa: BLE001
        props["row_breaks"] = "unknown"
        props["col_breaks"] = "unknown"

    # data validation / conditional formatting / hyperlinks
    try:
        dvs = []
        for dv in (ws.data_validations.dataValidation if ws.data_validations else []):
            dvs.append({
                "sqref": str(dv.sqref),
                "type": dv.type,
                "operator": dv.operator,
                "formula1": dv.formula1,
                "formula2": dv.formula2,
                "allow_blank": dv.allowBlank,
            })
        props["data_validations"] = sorted(dvs, key=lambda d: d["sqref"])
    except Exception:  # noqa: BLE001
        props["data_validations"] = "unknown"
    try:
        cfs = []
        for cf in ws.conditional_formatting:
            cfs.append({
                "sqref": str(cf.sqref),
                "rules": [
                    {"type": r.type, "operator": getattr(r, "operator", None),
                     "formula": list(getattr(r, "formula", []) or []),
                     "style": _dxf_style(getattr(r, "dxf", None), palette)}
                    for r in cf.rules
                ],
            })
        props["conditional_formats"] = sorted(cfs, key=lambda d: d["sqref"])
    except Exception:  # noqa: BLE001
        props["conditional_formats"] = "unknown"
    return props


def _is_styled_empty(cell) -> bool:
    """A value-less cell carrying visible formatting: any border side, bold,
    a fill with a fill type, or a number format other than General.
    `cell.has_style` alone is too broad (every cell touched by a style row)."""
    try:
        if not getattr(cell, "has_style", False):
            return False
        font = cell.font
        if font is not None and getattr(font, "bold", None):
            return True
        fill = cell.fill
        if fill is not None and getattr(fill, "fill_type", None):
            return True
        nf = cell.number_format
        if nf and nf != "General":
            return True
        border = cell.border
        if border is not None and any(
            getattr(getattr(border, side, None), "style", None)
            for side in ("top", "bottom", "left", "right")
        ):
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _dxf_style(dxf, palette=None) -> Optional[dict]:
    """The style a conditional-format rule applies (openpyxl resolves dxfId
    on load): font colour, fill and bold. None when the rule carries none.
    Solid dxf fills usually store the colour in bgColor, so both are read."""
    if dxf is None:
        return None
    out: dict[str, Any] = {}
    try:
        font = getattr(dxf, "font", None)
        if font is not None:
            fc = _color_text(getattr(font, "color", None), palette)
            if fc and not fc.endswith(":00000000"):
                out["font"] = fc
            if getattr(font, "bold", None):
                out["bold"] = True
        fill = getattr(dxf, "fill", None)
        if fill is not None:
            bg = _color_text(getattr(fill, "bgColor", None), palette)
            fg = _color_text(getattr(fill, "fgColor", None), palette)
            chosen = None
            for cand in (bg, fg):
                if cand and not cand.endswith(":00000000"):
                    chosen = cand
                    break
            if chosen:
                out["fill"] = chosen
    except Exception:  # noqa: BLE001
        return out or None
    return out or None


def extract_workbook_properties(workbook, excel_file_path, name_map: dict | None = None) -> dict:
    """Everything the rubric grades that is not a cell value.

    `name_map` maps original sheet names to the output (filtered/safe) names
    the CSVs were saved under; sheets skipped by the filter map to None.
    """
    path = Path(excel_file_path)
    wb: dict[str, Any] = {
        "filename": path.name,
        "bytes": _safe(lambda: path.stat().st_size, None),
        # load_workbooks never sets keep_vba, so vba_archive is always None;
        # the zip listing is the reliable test and needs no load-flag change.
        "has_vba": _safe(lambda: _zip_has_vba(path), None),
        "calc_mode": _safe(lambda: workbook.calculation.calcMode, None),
        "full_calc_on_load": _safe(lambda: workbook.calculation.fullCalcOnLoad, None),
        "iterative_calc": _safe(lambda: workbook.calculation.iterate, None),
        "defined_names": [],
        "external_links": [],
        "active_sheet": _safe(lambda: workbook.active.title if workbook.active else None, None),
    }
    try:
        names = []
        dn = workbook.defined_names
        items = dn.items() if hasattr(dn, "items") else [(d.name, d) for d in dn.definedName]
        for name, d in items:
            names.append({"name": name, "refers_to": getattr(d, "attr_text", None), "scope": None,
                          "hidden": bool(getattr(d, "hidden", False))})
        for ws in workbook.worksheets:
            local = getattr(ws, "defined_names", None)
            if local and hasattr(local, "items"):
                for name, d in local.items():
                    names.append({"name": name, "refers_to": getattr(d, "attr_text", None),
                                  "scope": ws.title,
                                  "hidden": bool(getattr(d, "hidden", False))})
        wb["defined_names"] = sorted(names, key=lambda d: (d["scope"] or "", d["name"]))
    except Exception:  # noqa: BLE001
        wb["defined_names"] = "unknown"
    try:
        wb["external_links"] = sorted(
            str(getattr(getattr(l, "file_link", None), "Target", None) or "?")
            for l in workbook._external_links
        )
    except Exception:  # noqa: BLE001
        wb["external_links"] = "unknown"

    try:
        from . import theme_palette as _theme
    except ImportError:  # bare-module import path
        import theme_palette as _theme
    palette = _safe(lambda: _theme.load_palette(workbook), None)

    sheets = []
    name_map = name_map or {}
    for i, ws in enumerate(workbook._sheets if hasattr(workbook, "_sheets") else workbook.worksheets, 1):
        sheets.append(_sheet_properties(ws, i, name_map.get(ws.title, ws.title), palette))
    return {"schema": SCHEMA_VERSION, "workbook": wb, "sheets": sheets}


def _zip_has_vba(path: Path) -> Optional[bool]:
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            return "xl/vbaProject.bin" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return None


ORIGIN_FILENAME = "_attempt_origin.json"


def load_origin(directory) -> Optional[dict]:
    """The attempt's provenance sidecar written by grade_from_db.setup_task_folder
    (original filename + source URI); None when absent (v1, local folders)."""
    if not directory:
        return None
    p = Path(directory) / ORIGIN_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_properties(output_dir: Path, props: dict) -> Path:
    p = Path(output_dir) / FILENAME
    p.write_text(json.dumps(props, indent=2, default=str), encoding="utf-8")
    return p


def load_properties(directory) -> Optional[dict]:
    if not directory:
        return None
    p = Path(directory) / FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"  [properties] unreadable {p}: {e}")
        return None


# ---------------------------------------------------------------------------
# Ordering + rendering
# ---------------------------------------------------------------------------


def order_file_list(file_list: list[str], props: Optional[dict]) -> list[str]:
    """Order `*_full.csv` names by the workbook's TRUE tab order.

    Names not present in the properties (older caches, filtered sheets) keep
    an alphabetical order after the known ones, so a v2 cache degrades to
    exactly the old listing.
    """
    if not props:
        return sorted(file_list)
    rank = {}
    for s in props.get("sheets", []):
        out_name = s.get("output_name")
        if out_name:
            from .excel_utils import create_safe_filename  # local import: avoid a cycle at import time

            rank[f"{create_safe_filename(out_name)}_full.csv"] = s["index"]
    known = sorted((f for f in file_list if f in rank), key=lambda f: rank[f])
    unknown = sorted(f for f in file_list if f not in rank)
    return known + unknown


def _group_text(runs, col: bool) -> str:
    """Outline runs -> `12-20=L1, 25-30=L2` / `C:F=L1`; 'none' / 'unknown'."""
    if runs == "unknown":
        return "unknown"
    if not runs:
        return "none"
    return (_col_runs_text if col else _row_runs_text)(
        runs[:_MAX_LIST], fmt=lambda lvl: f"L{lvl}"
    ) + (f", (+{len(runs) - _MAX_LIST} more runs)" if len(runs) > _MAX_LIST else "")


def _hidden_text(indexes, groups, col: bool) -> str:
    """Hidden ranges, each marked `(grouped)` when it sits inside an outline
    group — the rubric prefers collapsed groups to plain hiding (check 93)."""
    if indexes == "unknown":
        return "unknown"
    if not indexes:
        return "none"
    grouped: set = set()
    if isinstance(groups, list):
        for g in groups:
            grouped.update(range(int(g["first"]), int(g["last"]) + 1))
    parts = []
    for r in _runs([(i, True) for i in indexes]):
        a, b = r["first"], r["last"]
        if col:
            a_t, b_t = get_column_letter(a), get_column_letter(b)
            label = a_t if a == b else f"{a_t}:{b_t}"
        else:
            label = str(a) if a == b else f"{a}-{b}"
        if all(i in grouped for i in range(a, b + 1)):
            label += " (grouped)"
        parts.append(label)
    return ", ".join(parts)


def _cf_rule_text(rule: dict) -> str:
    """`cellIs equal "MODEL OK" -> fill rgb:C6EFCE font rgb:9C0006 bold`."""
    bits = [rule.get("type") or "?"]
    if rule.get("operator"):
        bits.append(str(rule["operator"]))
    formulas = rule.get("formula") or []
    if formulas:
        bits.append(", ".join(str(f)[:40] for f in formulas[:2]))
    style = rule.get("style")
    if isinstance(style, dict) and style:
        st = []
        if style.get("fill"):
            st.append(f"fill {style['fill']}")
        if style.get("font"):
            st.append(f"font {style['font']}")
        if style.get("bold"):
            st.append("bold")
        bits.append("-> " + " ".join(st))
    return " ".join(bits)


def _fmt_list(items, limit=_MAX_LIST, fn=str) -> str:
    if items == "unknown":
        return "unknown"
    if not items:
        return "none"
    shown = [fn(x) for x in items[:limit]]
    more = len(items) - limit
    return "; ".join(shown) + (f"; (+{more} more)" if more > 0 else "")


def render_properties_text(
    props: Optional[dict],
    listed_files: Optional[set] = None,
    origin: Optional[dict] = None,
) -> str:
    """Compact deterministic text for the seed prompt.

    `listed_files` (the `*_full.csv` names actually served) marks sheets
    whose CSV was dropped (ignored/filtered) so the judge is not sent
    looking for a file that is not there. `origin` (attempt only) is the
    provenance sidecar from setup_task_folder: the attempt is staged as
    ai_attempt.xlsx, so the delivered filename/extension (check 77) is
    only known from it.
    """
    if not props:
        return "  (workbook properties not available — older extraction cache)"
    wb = props.get("workbook", {})
    lines = []
    size = wb.get("bytes")
    size_txt = f"{size / 1024:.0f} KB" if isinstance(size, (int, float)) else "unknown size"
    calc_mode = wb.get("calc_mode")
    original = (origin or {}).get("original_filename")
    lines.append(
        f"Workbook {wb.get('filename', '?')} ({size_txt})"
        f"{f'; original filename: {original}' if original else ''}; calc mode: "
        f"{calc_mode if calc_mode and calc_mode != 'unknown' else 'auto (Excel default, none set)'}"
        f"{' (full calc on load)' if wb.get('full_calc_on_load') else ''}; iterative calc: "
        f"{'on' if wb.get('iterative_calc') else ('off' if wb.get('iterative_calc') is not None else 'unknown')}; "
        f"VBA: {'yes' if wb.get('has_vba') else ('no' if wb.get('has_vba') is not None else 'unknown')}; "
        f"active sheet: {wb.get('active_sheet') or 'unknown'}"
    )
    dn = wb.get("defined_names")
    if isinstance(dn, list):
        # Hidden names (add-ins such as @RISK plant dozens per file) are
        # counted, not listed, so the model's own names keep the slots
        # (rubric_9 checks 9/29: hidden add-in names do not count against
        # the Name Manager). The JSON keeps every name.
        user_names = [d for d in dn if not str(d.get("name", "")).startswith(_SYSTEM_NAME_PREFIXES)]
        n_sys = len(dn) - len(user_names)
        visible = [d for d in user_names if not d.get("hidden")]
        n_hidden = len(user_names) - len(visible)
        footnote = [f"+{n_sys} add-in/system"] * bool(n_sys) + [f"+{n_hidden} hidden"] * bool(n_hidden)
        lines.append(
            "Defined names: " + _fmt_list(
                visible,
                fn=lambda d: (
                    f"{d['name']}{' (' + d['scope'] + ')' if d.get('scope') else ''}"
                    f" -> {d.get('refers_to')}"
                ),
            )
            + (f" [{', '.join(footnote)} names not listed]" if footnote else "")
        )
    else:
        lines.append("Defined names: unknown")
    lines.append("External links: " + _fmt_list(wb.get("external_links")))
    lines.append("Sheets in TRUE TAB ORDER (index. name [state]):")
    from .excel_utils import create_safe_filename

    for s in props.get("sheets", []):
        state = s.get("state", "visible")
        tag = f" [{state.upper()}]" if state and state != "visible" else ""
        if s.get("kind") == "chartsheet":
            lines.append(f"  {s['index']}. {s['name']}{tag} — chart sheet (no cell data)")
            continue
        out = s.get("output_name")
        served = ""
        if out is None:
            served = " — not served (filtered out)"
        elif listed_files is not None and f"{create_safe_filename(out)}_full.csv" not in listed_files:
            served = " — not served (ignored sheet)"
        elif out != s["name"]:
            served = f" — served as {out}_full.csv"
        head = (
            f"  {s['index']}. {s['name']}{tag}{served}: "
            f"{s.get('max_row')}x{s.get('max_column')} (used {s.get('used_range') or 'empty'}; "
            f"{s.get('n_values', '?')} values, {s.get('n_formulas', '?')} formulas"
            f"{', ' + str(s['n_spill_anchors']) + ' spill/array ranges' if s.get('n_spill_anchors') else ''})"
        )
        lines.append(head)
        fp = s.get("freeze_panes")
        detail = [
            f"freeze panes: {fp or 'none'}",
            f"gridlines: {'on' if s.get('gridlines') in (None, True) else 'off'}",
            f"zoom: {s.get('zoom') or 100}",
            f"tab color: {s.get('tab_color') or 'none'}",
            f"protected: {'yes' if s.get('protected') else 'no'}",
            f"merged ranges: {len(s.get('merged_ranges') or []) if s.get('merged_ranges') != 'unknown' else 'unknown'}",
            "active cell: " + str(s.get("active_cell") or "unknown")
            + (f" (opens scrolled to {s['top_left_cell']})"
               if s.get("top_left_cell") and s.get("top_left_cell") != "A1" else ""),
        ]
        lines.append("     " + "; ".join(detail))
        se = s.get("styled_empty_cells")
        if isinstance(se, dict):
            n_se = int(se.get("count", 0) or 0)
            ex = list(se.get("examples") or [])
            se_txt = "none" if n_se == 0 else (
                f"{n_se} (e.g. {', '.join(ex)}{', ...' if n_se > len(ex) else ''})"
            )
        else:
            se_txt = "unknown" if se == "unknown" else "none"
        lines.append("     styled empty cells in used range: " + se_txt)
        hr, hc = s.get("hidden_rows", []), s.get("hidden_cols", [])
        rg, cg = s.get("row_groups", []), s.get("col_groups", [])
        lines.append(
            "     hidden rows: " + _hidden_text(hr, rg, col=False)
            + "; hidden cols: " + _hidden_text(hc, cg, col=True)
        )
        lines.append(
            "     grouped rows: " + _group_text(rg, col=False)
            + "; grouped cols: " + _group_text(cg, col=True)
        )
        rb, cb = s.get("row_breaks", []), s.get("col_breaks", [])
        lines.append(
            "     page breaks: rows " + ("unknown" if rb == "unknown" else (", ".join(str(b) for b in rb) if rb else "none"))
            + "; cols " + ("unknown" if cb == "unknown" else (", ".join(get_column_letter(b) for b in cb) if cb else "none"))
        )
        cw = s.get("column_widths")
        dcw = s.get("default_col_width")
        lines.append(
            "     column widths: "
            + (_col_runs_text(cw[:_MAX_LIST]) + (f", (+{len(cw) - _MAX_LIST} more runs)" if len(cw) > _MAX_LIST else "")
               if isinstance(cw, list) and cw else ("unknown" if cw == "unknown" else "all default"))
            + f" (default {dcw if dcw else 8.43})"
        )
        rh = s.get("row_heights")
        lines.append(
            "     custom row heights: "
            + (_row_runs_text(rh[:_MAX_LIST]) + (f", (+{len(rh) - _MAX_LIST} more runs)" if len(rh) > _MAX_LIST else "")
               if isinstance(rh, list) and rh else ("unknown" if rh == "unknown" else "none"))
        )
        lines.append(
            "     data validation: " + _fmt_list(
                s.get("data_validations"),
                fn=lambda d: f"{d['sqref']} {d.get('type') or '?'}"
                             f"{' ' + str(d.get('formula1')) if d.get('formula1') else ''}",
            )
        )
        lines.append(
            "     conditional formats: " + _fmt_list(
                s.get("conditional_formats"),
                fn=lambda d: f"{d['sqref']} ({'; '.join(_cf_rule_text(r) for r in d.get('rules', []))})",
            )
        )
        lines.append(
            "     comments/notes: " + _fmt_list(
                s.get("comments"),
                fn=lambda d: f"{d['ref']}: \"{(d.get('text') or '').strip()[:80]}\"",
            )
        )
        lines.append("     hyperlinks: " + _fmt_list(s.get("hyperlinks"), fn=lambda d: f"{d['ref']} -> {d.get('target')}"))
        lines.append(
            f"     print: area {s.get('print_area') or 'none'}; title rows {s.get('print_title_rows') or 'none'}; "
            f"orientation {s.get('page_orientation') or 'default'}; fit to page: "
            f"{'yes' if s.get('fit_to_page') else 'no'}"
        )
    return "\n".join(lines)
