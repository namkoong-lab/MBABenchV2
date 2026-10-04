"""Grade one workbook and print the verdicts as JSON.

    /Users/patrick/MBABenchV2/.venv/bin/python -m detchecks.tools.grade_one <path> [--checks 92,74]
        [--value-path copy.xlsx] [--recalc [--no-excel] [--workdir DIR]]

--recalc runs the recalculation pipeline (core/recalc.py) for value checks: LibreOffice copy for
non-Excel files, Excel copy when LibreOffice has gaps.  It launches office applications, so run
it through heavy_run.py (also for any workbook over 5 MB).  Exit 0 on success; on GradingError
prints {"error": ..., "failures": {...}} and exits 2 - there is no fallback.
"""
from __future__ import annotations

import argparse
import json
import sys

from ..api import grade
from ..core.recalc import DEFAULT_WORKDIR, RecalcPolicy
from ..errors import GradingError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--checks", default=None, help="comma-separated rubric numbers (default: all registered)")
    ap.add_argument("--value-path", default=None, help="recalculation copy for formula values (manual override)")
    ap.add_argument("--recalc", action="store_true", help="run the recalculation pipeline for value checks")
    ap.add_argument("--no-excel", action="store_true", help="pipeline: never reroute to Excel (fail loudly instead)")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR, help="pipeline cache directory")
    args = ap.parse_args(argv)
    checks = [int(x) for x in args.checks.split(",")] if args.checks else None
    recalc = RecalcPolicy(workdir=args.workdir, excel_allowed=not args.no_excel) if args.recalc else None
    try:
        v = grade(args.path, checks=checks, value_path=args.value_path, recalc=recalc)
    except GradingError as e:
        print(json.dumps({"error": str(e), "failures": e.failures}, indent=2, ensure_ascii=False))
        return 2
    print(json.dumps(v, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
