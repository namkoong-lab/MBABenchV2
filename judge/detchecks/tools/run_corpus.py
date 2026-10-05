"""Sanity run over delivered attempts (step 3 of the per-check sequence: look for crashes and
surprising fail rates; not a gate).

    cd /Users/patrick/MBABench-deterministic-checks
    python3 heavy_run.py -- /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tools.run_corpus \
        --checks 92,74 [--touch] [--glob 'corpus/attempts/*/'] [--recalc [--no-excel] [--lo-timeout 600]]
        [--excel-saved-only] [--llm-check 22] [--out detchecks/out/sanity_22.json]

Grades every spreadsheet file directly inside each matched attempt folder (the delivered file;
recalc_libreoffice/ copies are never graded).  task_meta comes from _attempt_origin.json when
present (delivered_filename, attempt_id).  --touch additionally streams every cell of every
worksheet once (detchecks.tools.bench.TouchAll).  --recalc runs the recalculation pipeline
(core/recalc.py) for value checks: LibreOffice copy for non-Excel files (one soffice process at a
time, per-file timeout; a timeout is recorded as an error and the run continues), Excel copy when
LibreOffice has gaps.  --excel-saved-only skips files not saved by Excel (GPT-6 rule: no
LibreOffice over the 303 files) and reports the count skipped.  --llm-check N adds the LLM's
latest verdict for check N from verdicts.json (sol_latest_pass) and the agreement.  Files run one
after another in this single process (no workers).  Writes detchecks/out/corpus_<checks>.json;
exit 1 if any file raised GradingError.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time

from ..api import Engine, grade
from ..checks import REGISTRY
from ..core.package import Package
from ..core.recalc import DEFAULT_WORKDIR, RecalcPolicy
from ..core.values import detect_writer
from ..errors import GradingError
from .bench import TouchAll, peak_rss_mb

BASE = "/Users/patrick/MBABench-deterministic-checks"
EXTS = (".xlsx", ".xlsm", ".xlsb", ".xls", ".xltx", ".xltm", ".csv", ".ods")
# verdict stats kept in the output rows (No formula errors (22): error cells; Reasonable column widths (70): the
# switches and what decided; Sufficient column widths (69): which rule decided - '####' numbers or cut-off text,
# unwrapped (a) / wrapped (b) - and what stops the cut-off text)
KEEP_STATS = ("error_cells_by_code", "implicit_intersection_cells", "typed_error_constants", "formula_cells",
              "examples", "switches", "tests_failed", "n_outlier_tags", "n_outlier_tags_unequal", "outlier_tags",
              "n_outlier_band_columns", "outlier_band_columns", "brief_fails", "undecided_cells",
              "undecided_examples", "sheets_with_overlapping_cols", "second_pass_sheets", "unknown_faces",
              "mistakes_by_rule", "sure_overflows", "text_cut_off", "wrapped_cut_off", "text_band", "wrapped_band",
              "text_blockers", "wrapped_merged_in_auto_rows")


def delivered_files(pattern: str) -> list[str]:
    out = []
    for d in sorted(glob.glob(os.path.join(BASE, pattern) if not os.path.isabs(pattern) else pattern)):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p) and f.lower().endswith(EXTS) and not f.startswith("~$"):
                out.append(p)
    return out


def task_meta_for(path: str) -> dict:
    d = os.path.dirname(path)
    meta = {"delivered_filename": os.path.basename(path)}
    p = os.path.join(d, "_attempt_origin.json")
    if os.path.exists(p):
        try:
            with open(p) as fh:
                o = json.load(fh)
            meta.update({k: o[k] for k in ("original_filename", "attempt_id", "source") if k in o})
            if o.get("original_filename"):
                meta["delivered_filename"] = o["original_filename"]
        except (OSError, ValueError):
            pass
    return meta


def llm_verdict(path: str, number: int):
    """(sol_latest_pass, rationale text) for check `number` from the attempt's verdicts.json, or None."""
    p = os.path.join(os.path.dirname(path), "verdicts.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as fh:
            v = json.load(fh)
        chk = v["checks"][str(number)]
        text = None
        if chk.get("sol"):
            text = (chk["sol"][-1].get("rationale") or {}).get("text")
        return {"sol_latest_pass": chk.get("sol_latest_pass"), "fable_pass": chk.get("fable_pass"),
                "suitability": chk.get("suitability"), "rationale": (text or "")[:600]}
    except (OSError, ValueError, KeyError):
        return None


def writer_of(path: str) -> str:
    try:
        pkg = Package.open(path)
        try:
            return detect_writer(pkg)[0] if pkg.is_spreadsheetml else f"unparsed:{pkg.format}"
        finally:
            pkg.close()
    except GradingError as e:
        return f"unreadable: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checks", default=None)
    ap.add_argument("--glob", default="corpus/attempts/*/")
    ap.add_argument("--touch", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--recalc", action="store_true", help="run the recalculation pipeline for value checks")
    ap.add_argument("--no-excel", action="store_true", help="pipeline: never reroute to Excel")
    ap.add_argument("--allow-excel", action="store_true", help="pipeline: allow the Excel reroute (OFF by default; Patrick 2026-10-04: agents never launch Excel)")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR)
    ap.add_argument("--lo-timeout", type=float, default=600.0)
    ap.add_argument("--excel-timeout", type=float, default=600.0)
    ap.add_argument("--excel-saved-only", action="store_true", help="skip files not saved by Excel (count them)")
    ap.add_argument("--llm-check", type=int, default=None, help="add the LLM verdict for this check from verdicts.json")
    args = ap.parse_args(argv)
    numbers = sorted(REGISTRY) if not args.checks else [int(x) for x in args.checks.split(",")]
    recalc = (RecalcPolicy(workdir=args.workdir, excel_allowed=bool(args.allow_excel and not args.no_excel), lo_timeout_s=args.lo_timeout,
                           excel_timeout_s=args.excel_timeout) if args.recalc else None)
    files = delivered_files(args.glob)
    rows = []
    errors = 0
    skipped = 0
    decisions = collections.Counter()
    agree = collections.Counter()
    sources = collections.Counter()
    t_all = time.perf_counter()
    for p in files:
        row = {"path": os.path.relpath(p, BASE), "size": os.path.getsize(p), "writer": writer_of(p)}
        if args.excel_saved_only and row["writer"] != "excel":
            skipped += 1
            row["skipped"] = "not saved by Excel"
            rows.append(row)
            print(f"skip  {row['path'][:80]:80s} writer={row['writer']}", flush=True)
            continue
        t0 = time.perf_counter()
        try:
            v = grade(p, checks=numbers, task_meta=task_meta_for(p), recalc=recalc)
            row["verdicts"] = {k: {"decision": x["decision"], "live": x.get("live", True),
                                   "n_mistakes": x["stats"].get("n_mistakes"),
                                   "mistakes": x["mistakes"][:8],
                                   "values": x["stats"].get("values"),
                                   "stats": {kk: vv for kk, vv in x["stats"].items() if kk in KEEP_STATS}}
                               for k, x in v.items()}
            for k, x in v.items():
                decisions[(k, x["decision"])] += 1
                vals = x["stats"].get("values")
                if vals:
                    sources[vals.get("source")] += 1
        except GradingError as e:
            errors += 1
            row["error"] = str(e)
            row["failures"] = dict(e.failures)
            row["partial_verdicts"] = {k: x["decision"] for k, x in (e.verdicts or {}).items()}
        row["seconds"] = round(time.perf_counter() - t0, 2)
        if args.llm_check is not None:
            lv = llm_verdict(p, args.llm_check)
            row["llm"] = lv
            key = REGISTRY[args.llm_check].key if args.llm_check in REGISTRY else None
            py = (row.get("verdicts") or {}).get(key, {}).get("decision") if key else None
            if lv is not None and lv.get("sol_latest_pass") is not None and py is not None:
                llm = "pass" if lv["sol_latest_pass"] else "fail"
                row["agreement"] = "agree" if llm == py else f"py_{py}/llm_{llm}"
                agree[row["agreement"]] += 1
            elif py is None:
                agree["py_error"] += 1
            else:
                agree["no_llm_verdict"] += 1
        if args.touch:
            t0 = time.perf_counter()
            chk = TouchAll()
            try:
                Engine(p, [chk]).run()
                row["touch"] = chk.stats
            except GradingError as e:
                errors += 1
                row["touch_error"] = str(e)
            row["touch_seconds"] = round(time.perf_counter() - t0, 2)
        rows.append(row)
        status = "ERROR" if ("error" in row or "touch_error" in row) else "ok"
        brief = " ".join(f"{k.split('/')[-1][:14]}={x['decision']}({x['n_mistakes']})"
                         + (f"[{x['values']['source']}]" if x.get("values") else "")
                         for k, x in row.get("verdicts", {}).items())
        touch = f" cells={row['touch']['cells']}" if row.get("touch") else ""
        agr = f" {row['agreement']}" if row.get("agreement") else ""
        print(f"{status:5s} {row['seconds']:7.2f}s {row.get('touch_seconds', 0):6.2f}s {row['path'][:70]:70s} "
              f"{row['writer'][:11]:11s} {brief}{touch}{agr}"
              + (f"  {row.get('error') or row.get('touch_error')}"[:400] if status == "ERROR" else ""), flush=True)
    summary = {"files": len(files), "graded": len(files) - skipped - errors, "errors": errors, "skipped": skipped,
               "seconds": round(time.perf_counter() - t_all, 1), "peak_rss_mb": round(peak_rss_mb(), 1),
               "decisions": {f"{k} = {d}": n for (k, d), n in sorted(decisions.items())},
               "value_sources": dict(sources), "llm_agreement": dict(agree),
               "recalc_pipeline": recalc is not None, "excel_allowed": bool(recalc and recalc.excel_allowed),
               "error_causes": collections.Counter(
                   ("timeout" if "timed out" in r["error"] else "excel_needed" if "Excel recalculation required" in r["error"]
                    else "untrusted" if "untrusted" in r["error"] else "other") for r in rows if "error" in r)}
    print(json.dumps(summary, indent=1))
    out = args.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out",
                                   f"corpus_{'_'.join(map(str, numbers))}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, indent=1)
    print(f"wrote {out}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
