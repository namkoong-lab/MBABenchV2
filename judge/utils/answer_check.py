"""Harness answer check (Phase B of the 2026-08 judge update) — score-neutral.

Extracts the 'Questions'-sheet answers from the attempt and the golden
solution workbooks and compares them deterministically, recording verdicts
alongside (never instead of) the LLM judge's Accuracy check. Two parallel
signals; no effect on the 0-100 score.

Convention (benchmark v2 Questions-era cohorts): each workbook has one or
more sheets whose name starts with "Questions" (case-insensitive); questions
sit in column A; a header cell reading "Answers" (rows 1-10, columns A-F)
anchors the answer column; answers occupy the rows below the header. A
workbook without the convention yields status "convention_not_found" —
expected for older cohorts, never an error.

Values come from cached formula results (data_only=True). If any answer cell
holds a formula with no cached value — typical of openpyxl-produced attempts
— the workbook is recalculated once through LibreOffice into a temp copy and
re-read (recalc_used records this). Numeric verdicts use the locked
tolerance |a-b| <= max(1e-9, 1e-6*max(|a|,|b|)); text compares casefolded
and whitespace-normalized.
"""

import json
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, time as dt_time
from pathlib import Path

import openpyxl

try:
    from .logger import logger
    from .misc_utils import load_env_var
except ImportError:  # imported as a bare module (utils/ on sys.path)
    from logger import logger
    from misc_utils import load_env_var

ABS_TOL = 1e-9
REL_TOL = 1e-6
HEADER_SCAN_ROWS = 10
HEADER_SCAN_COLS = 6  # A-F


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().casefold()


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _coerce_number(v):
    """A numeric value for comparison, or None. Plain numeric strings count;
    no unit/percent stripping — convention mismatches must stay visible."""
    if _is_number(v):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _display(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date, dt_time)):
        return v.isoformat()
    return str(v)


def numbers_match(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


def compare_values(expected, got) -> dict:
    """Verdict dict for one (expected, got) pair (labels handled by caller)."""
    out = {
        "expected": expected if _is_number(expected) or isinstance(expected, (str, bool)) else _display(expected),
        "got": got if _is_number(got) or isinstance(got, (str, bool)) else _display(got),
        "expected_display": _display(expected),
        "got_display": _display(got),
        "verdict": None,
        "abs_delta": None,
        "rel_delta": None,
    }
    if got is None and expected is None:
        out["verdict"] = "missing"
        out["mismatch_type"] = "both_missing"
        return out
    if got is None:
        out["verdict"] = "missing"
        return out
    if expected is None:
        out["verdict"] = "missing"
        out["mismatch_type"] = "missing_expected"
        return out

    en, gn = _coerce_number(expected), _coerce_number(got)
    if en is not None and gn is not None:
        out["abs_delta"] = abs(en - gn)
        out["rel_delta"] = (
            abs(en - gn) / max(abs(en), abs(gn))
            if max(abs(en), abs(gn)) > 0
            else 0.0
        )
        out["verdict"] = "match" if numbers_match(en, gn) else "mismatch"
        return out
    if en is None and gn is None:
        # both non-numeric -> normalized text comparison (bools compare as text)
        out["verdict"] = (
            "match" if _norm_text(_display(expected)) == _norm_text(_display(got))
            else "mismatch"
        )
        return out
    out["verdict"] = "mismatch"
    out["mismatch_type"] = "type_mismatch"  # text vs number — not comparable
    return out


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def _questions_sheets(wb) -> list[str]:
    return sorted(
        name for name in wb.sheetnames
        if name.strip().casefold().startswith("questions")
    )


def _find_answers_header(ws) -> tuple[int, int] | None:
    for r in range(1, min(HEADER_SCAN_ROWS, ws.max_row or 1) + 1):
        for c in range(1, HEADER_SCAN_COLS + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and v.strip().casefold() == "answers":
                return r, c
    return None


def _uncached_answer_cells(path: Path, targets: list[tuple[str, int, int]]) -> int:
    """How many target cells hold a formula whose cached value is missing."""
    wb = openpyxl.load_workbook(str(path), data_only=False)
    wb_vals = openpyxl.load_workbook(str(path), data_only=True)
    n = 0
    for sheet, row, col in targets:
        v = wb[sheet].cell(row=row, column=col).value
        is_formula = (isinstance(v, str) and v.startswith("=")) or isinstance(
            v, openpyxl.worksheet.formula.ArrayFormula
        )
        if is_formula and wb_vals[sheet].cell(row=row, column=col).value is None:
            n += 1
    wb.close()
    wb_vals.close()
    return n


def _recalculate_copy(xlsx_path: Path, outdir: Path) -> Path:
    """Re-save through LibreOffice to populate cached formula values."""
    soffice = load_env_var("PATHS_LIBREOFFICE_PATH", required=True)
    subprocess.run(
        [soffice, "--headless", "--calc", "--convert-to", "xlsx",
         "--outdir", str(outdir), str(xlsx_path)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    out = outdir / xlsx_path.name
    if not out.exists():
        raise FileNotFoundError(f"LibreOffice produced no output for {xlsx_path}")
    return out


def extract_answers(xlsx_path: Path, allow_recalc: bool = True) -> dict:
    """Extract {status, rows: [{sheet, row, label, value}], recalc_used}.

    Rows are concatenated across Questions* sheets in name order; a row is
    kept when its column-A label or its answer cell is non-empty.
    """
    xlsx_path = Path(xlsx_path)
    result = {"status": "ok", "rows": [], "recalc_used": False}

    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    sheets = _questions_sheets(wb)
    if not sheets:
        wb.close()
        result["status"] = "convention_not_found"
        result["reason"] = "no sheet named Questions*"
        return result

    # Locate the header per sheet and collect target cells.
    anchors = {}
    for name in sheets:
        anchor = _find_answers_header(wb[name])
        if anchor:
            anchors[name] = anchor
    if not anchors:
        wb.close()
        result["status"] = "convention_not_found"
        result["reason"] = "no 'Answers' header in rows 1-10, columns A-F"
        return result

    targets = []
    for name, (hrow, hcol) in anchors.items():
        ws = wb[name]
        for r in range(hrow + 1, (ws.max_row or hrow) + 1):
            targets.append((name, r, hcol))

    # Recalc once if any answer cell is a formula without a cached value.
    if allow_recalc:
        n_uncached = _uncached_answer_cells(xlsx_path, targets)
        if n_uncached:
            logger.info(
                f"  [answer_check] {xlsx_path.name}: {n_uncached} answer cell(s) "
                f"lack cached values — recalculating via LibreOffice"
            )
            tmpdir = Path(tempfile.mkdtemp(prefix="answer_check_recalc_"))
            try:
                recalced = _recalculate_copy(xlsx_path, tmpdir)
                wb.close()
                wb = openpyxl.load_workbook(str(recalced), data_only=True)
                result["recalc_used"] = True
            except Exception as e:  # recalc is best-effort; report what we have
                logger.warning(f"  [answer_check] recalc failed: {e}")
                result["recalc_error"] = str(e)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

    for name, (hrow, hcol) in anchors.items():
        ws = wb[name]
        for r in range(hrow + 1, (ws.max_row or hrow) + 1):
            label = ws.cell(row=r, column=1).value
            value = ws.cell(row=r, column=hcol).value
            if (label is None or str(label).strip() == "") and value is None:
                continue
            result["rows"].append(
                {
                    "sheet": name,
                    "row": r,
                    "label": str(label).strip() if label is not None else None,
                    "value": value if _is_number(value) or isinstance(value, (str, bool)) else _display(value),
                }
            )
    wb.close()
    if not result["rows"]:
        result["status"] = "convention_not_found"
        result["reason"] = "Answers header found but no question rows below it"
    return result


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def compare_answer_sets(solution: dict, attempt: dict) -> dict:
    """Align solution rows to attempt rows and compare each pair.

    Alignment: by normalized question label; rows whose labels don't align
    fall back to positional order and are flagged label_mismatch.
    """
    sol_rows, att_rows = solution["rows"], attempt["rows"]
    att_by_label: dict[str, list] = {}
    for row in att_rows:
        if row["label"]:
            att_by_label.setdefault(_norm_text(row["label"]), []).append(row)

    questions = []
    used = set()
    for i, srow in enumerate(sol_rows):
        arow, label_mismatch = None, False
        key = _norm_text(srow["label"]) if srow["label"] else None
        if key and att_by_label.get(key):
            arow = att_by_label[key].pop(0)
            used.add(id(arow))
        elif i < len(att_rows) and id(att_rows[i]) not in used:
            arow = att_rows[i]
            used.add(id(arow))
            label_mismatch = bool(
                srow["label"] and arow["label"]
                and _norm_text(srow["label"]) != _norm_text(arow["label"])
            )
        item = compare_values(srow["value"], arow["value"] if arow else None)
        item = {
            "label": srow["label"],
            "sheet": srow["sheet"],
            "row": srow["row"],
            **item,
        }
        if label_mismatch:
            item["label_mismatch"] = True
            item["attempt_label"] = arow["label"]
        questions.append(item)

    extra = [r for r in att_rows if id(r) not in used]
    verdicts = [q["verdict"] for q in questions]
    return {
        "status": "ok",
        "n_questions": len(questions),
        "n_match": verdicts.count("match"),
        "n_mismatch": verdicts.count("mismatch"),
        "n_missing": verdicts.count("missing"),
        "n_label_mismatch": sum(1 for q in questions if q.get("label_mismatch")),
        "n_extra_attempt_rows": len(extra),
        "questions": questions,
        "extra_attempt_rows": [
            {"sheet": r["sheet"], "row": r["row"], "label": r["label"],
             "value_display": _display(r["value"])}
            for r in extra
        ],
    }


# --------------------------------------------------------------------------
# Entry point (grade_from_db)
# --------------------------------------------------------------------------


def run_answer_check(attempt_xlsx, solution_xlsx, output_json_path=None) -> dict:
    """Full check: extract both sides, compare, optionally write the artifact.

    Never raises — an unexpected failure returns status "error" so the
    grading pipeline is never blocked by this score-neutral signal.
    """
    try:
        solution = extract_answers(Path(solution_xlsx), allow_recalc=True)
        attempt = extract_answers(Path(attempt_xlsx), allow_recalc=True)
        if solution["status"] != "ok" or attempt["status"] != "ok":
            result = {
                "status": "convention_not_found",
                "solution_status": solution["status"],
                "solution_reason": solution.get("reason"),
                "attempt_status": attempt["status"],
                "attempt_reason": attempt.get("reason"),
                "recalc_used": solution.get("recalc_used") or attempt.get("recalc_used", False),
            }
        else:
            result = compare_answer_sets(solution, attempt)
            result["recalc_used"] = bool(
                solution.get("recalc_used") or attempt.get("recalc_used")
            )
            result["solution_recalc_used"] = solution.get("recalc_used", False)
            result["attempt_recalc_used"] = attempt.get("recalc_used", False)
            if solution.get("recalc_error") or attempt.get("recalc_error"):
                result["recalc_error"] = (
                    solution.get("recalc_error") or attempt.get("recalc_error")
                )
    except Exception as e:  # noqa: BLE001 — deliberately never blocks grading
        logger.warning(f"  [answer_check] unexpected failure: {e}")
        result = {"status": "error", "error": str(e)}

    if output_json_path:
        try:
            Path(output_json_path).write_text(json.dumps(result, indent=2))
        except OSError as e:
            logger.warning(f"  [answer_check] could not write artifact: {e}")
    return result


def summary_block(result: dict) -> dict:
    """The compact block recorded in scored_results.answer_check."""
    keys = ("status", "n_questions", "n_match", "n_mismatch", "n_missing",
            "recalc_used")
    return {k: result.get(k) for k in keys if k in result}
