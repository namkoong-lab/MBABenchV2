"""Toy gate: grade every toy of each check with THAT check only and compare.

    cd judge && python -m detchecks.tools.run_toys --toys-root <toy workbooks dir> --checks 92,74

For each check NN, the toy folder is the one in --toys-root whose name starts with 'NN_'.
Every file in its Pass/ and Fail/ folders is graded (lock files '~$...' skipped; any
extension, so the 77 .xlsm/.xlsb/.xls toys are included).  Expected outcome: Pass/ -> pass,
Fail/ -> fail, unless detchecks/tools/expected.json overrides it (with a reason).
An expected.json entry may also list "failing_sheets": the exact set of sheets the check
must flag.  Then the sheets named by the verdict's mistake locations must equal that set,
so a gate whose toys are all expected to fail (62, strict A1) still tells a correct check
from one that fails everything.
Prints a matrix, writes detchecks/out/toys_<checks>.json, exits 1 on any mismatch or
GradingError (and 2 on usage errors).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from ..api import grade
from ..checks import REGISTRY
from ..core.package import Package
from ..core.recalc import DEFAULT_WORKDIR, RecalcPolicy
from ..errors import GradingError

DEFAULT_ROOT = os.environ.get("DETCHECKS_TOYS_ROOT")   # the toy workbooks are not in the repository
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "out")


def toy_folder(root: str, number: int) -> str | None:
    for name in sorted(os.listdir(root)):
        if re.match(rf"^{number}_", name) and os.path.isdir(os.path.join(root, name)):
            return os.path.join(root, name)
    return None


def toy_files(folder: str) -> list[tuple[str, str]]:
    out = []
    for sub in ("Pass", "Fail"):
        d = os.path.join(folder, sub)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.startswith("~$") or f.startswith(".") or not os.path.isfile(os.path.join(d, f)):
                continue
            out.append((sub, f))

    def key(x):
        m = re.match(r"T(\d+)", x[1])
        return (int(m.group(1)) if m else 999, x[0] != "Pass", x[1])
    return sorted(out, key=key)


def sheet_of_location(loc: str, sheet_names) -> str:
    """Sheet named by a mistake location: a bare sheet name, "'My Sheet'!A1:B2" or
    "Sheet1!A1".  ValueError when it names no sheet of the workbook."""
    names = set(sheet_names)
    if loc in names:
        return loc
    if loc.startswith("'"):
        out, i = [], 1
        while i < len(loc):
            if loc[i] == "'":
                if loc[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                name = "".join(out)
                if loc[i + 1:i + 2] == "!" and name in names:
                    return name
                break
            out.append(loc[i])
            i += 1
    else:
        k = loc.rfind("!")
        if k > 0 and loc[:k] in names:
            return loc[:k]
    raise ValueError(f"mistake location {loc!r} names no sheet of the workbook")


def failing_sheets_problem(verdict: dict, expected_sheets, sheet_names) -> str | None:
    """None when the sheets named by the verdict's mistakes are exactly expected_sheets,
    else a description of the difference."""
    if verdict["stats"].get("mistakes_truncated"):
        return "mistake list truncated: cannot compare failing_sheets"
    try:
        got = {sheet_of_location(m["location"], sheet_names) for m in verdict["mistakes"]}
    except ValueError as e:
        return str(e)
    exp = set(expected_sheets)
    if got == exp:
        return None
    return f"failing sheets differ: missing {sorted(exp - got)}, unexpected {sorted(got - exp)}"


def load_expected(path: str) -> dict:
    with open(path) as fh:
        data = json.load(fh)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checks", default=None, help="comma-separated rubric numbers (default: all registered)")
    ap.add_argument("--toys-root", default=DEFAULT_ROOT, required=DEFAULT_ROOT is None,
                    help="folder of per-check toy folders ('NN_<name>/Pass/', 'NN_<name>/Fail/')")
    ap.add_argument("--expected", default=os.path.join(HERE, "expected.json"))
    ap.add_argument("--out", default=None, help="JSON output path (default detchecks/out/toys_<checks>.json)")
    ap.add_argument("--no-recalc", action="store_true",
                    help="grade from the files' caches only (default: the recalc pipeline decides; every toy is "
                         "Excel-saved, so no office application is launched)")
    ap.add_argument("--no-excel", action="store_true", help="pipeline: never reroute to Excel")
    ap.add_argument("--allow-excel", action="store_true", help="pipeline: allow the Excel reroute (OFF by default: agents never launch Excel)")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR)
    args = ap.parse_args(argv)
    recalc = None if args.no_recalc else RecalcPolicy(workdir=args.workdir, excel_allowed=bool(args.allow_excel and not args.no_excel))

    numbers = sorted(REGISTRY) if not args.checks else [int(x) for x in args.checks.split(",") if x.strip()]
    unknown = [n for n in numbers if n not in REGISTRY]
    if unknown:
        print(f"not registered: {unknown} (registered: {sorted(REGISTRY)})", file=sys.stderr)
        return 2
    expected_over = load_expected(args.expected)
    rows = []
    bad = 0
    for n in numbers:
        folder = toy_folder(args.toys_root, n)
        if folder is None:
            print(f"#{n}: no toy folder '{n}_*' under {args.toys_root}", file=sys.stderr)
            rows.append({"check": n, "toy": None, "status": "NO_TOYS"})
            bad += 1
            continue
        key = REGISTRY[n].key
        for sub, fname in toy_files(folder):
            path = os.path.join(folder, sub, fname)
            exp = "pass" if sub == "Pass" else "fail"
            reason = None
            want_sheets = None
            ov = expected_over.get(str(n), {}).get(f"{sub}/{fname}")
            if ov:
                exp, reason = ov.get("expected", exp), ov.get("reason")
                want_sheets = ov.get("failing_sheets")
            t0 = time.perf_counter()
            row = {"check": n, "key": key, "folder": sub, "toy": fname, "expected": exp,
                   "override_reason": reason, "expected_failing_sheets": want_sheets}
            try:
                v = grade(path, checks=[n], recalc=recalc)[key]
                row.update(got=v["decision"], n_mistakes=v["stats"].get("n_mistakes", len(v["mistakes"])),
                           first_mistake=(f"{v['mistakes'][0]['location']}: {v['mistakes'][0]['description']}"
                                          if v["mistakes"] else ""),
                           summary=v["summary"])
                row["status"] = "OK" if v["decision"] == exp else "MISMATCH"
                if row["status"] == "OK" and want_sheets is not None:
                    with Package.open(path) as pkg:
                        names = [s_.name for s_ in pkg.sheets]
                    problem = failing_sheets_problem(v, want_sheets, names)
                    if problem:
                        row["status"] = "MISMATCH"
                        row["first_mistake"] = problem
            except GradingError as e:
                row.update(got="ERROR", n_mistakes=None, first_mistake="", summary=str(e), status="ERROR")
            row["seconds"] = round(time.perf_counter() - t0, 2)
            if row["status"] != "OK":
                bad += 1
            rows.append(row)
            fm = row["first_mistake"] if row["status"] != "ERROR" else row["summary"]
            print(f"{n:>3}  {sub:4s} {fname[:44]:44s} exp={exp:4s} got={row['got']:5s} "
                  f"{('' if row['n_mistakes'] is None else row['n_mistakes']):>4}  {row['seconds']:6.2f}s  "
                  f"{row['status']:8s} {fm[:110]}", flush=True)
            if reason:
                print(f"{'':10s}(override: {reason[:140]})")
    n_ok = sum(1 for r in rows if r.get("status") == "OK")
    print(f"\n{n_ok}/{len(rows)} toys as expected; {bad} mismatch/error(s).")
    out = args.out or os.path.join(OUT_DIR, f"toys_{'_'.join(str(n) for n in numbers)}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"checks": numbers, "toys_root": args.toys_root, "ok": n_ok, "total": len(rows),
                   "bad": bad, "recalc_pipeline": recalc is not None, "rows": rows}, fh, indent=1)
    print(f"wrote {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
