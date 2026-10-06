"""Run the recalculation pipeline on one workbook and print the plan (for testing the
LibreOffice and Excel steps on this machine).

    cd judge && python -m detchecks.tools.recalc_one <path>
        [--workdir DIR] [--no-excel] [--no-cache] [--lo-timeout 600] [--excel-timeout 600]
        [--force-excel]   # skip LibreOffice and the gap scan: Excel copy of this file (test of the Excel step)

Exit 0 and the ValuePlan summary as JSON; exit 2 with {"error": ...} on GradingError.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ..core.recalc import DEFAULT_WORKDIR, RecalcPolicy, ensure_values, excel_recalc, file_hash
from ..errors import GradingError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR)
    ap.add_argument("--no-excel", action="store_true")
    ap.add_argument("--allow-excel", action="store_true", help="pipeline: allow the Excel reroute (OFF by default; Patrick 2026-10-04: agents never launch Excel)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--lo-timeout", type=float, default=600.0)
    ap.add_argument("--excel-timeout", type=float, default=600.0)
    ap.add_argument("--force-excel", action="store_true")
    args = ap.parse_args(argv)
    pol = RecalcPolicy(workdir=args.workdir, excel_allowed=bool(args.allow_excel and not args.no_excel), use_cache=not args.no_cache,
                       lo_timeout_s=args.lo_timeout, excel_timeout_s=args.excel_timeout)
    try:
        if args.force_excel and not args.allow_excel:
            raise SystemExit("--force-excel also needs --allow-excel (Excel is off by default; agents never launch Excel)")
        if args.force_excel:
            out_dir = os.path.join(args.workdir, file_hash(args.path), "excel_forced")
            t0 = time.perf_counter()
            out = excel_recalc(args.path, out_dir, pol)
            print(json.dumps({"source": "excel (forced)", "value_path": out,
                              "excel_s": round(time.perf_counter() - t0, 2)}, indent=1))
            return 0
        plan = ensure_values(args.path, args.workdir, pol)
    except GradingError as e:
        print(json.dumps({"error": str(e)}, indent=1))
        return 2
    print(json.dumps(plan.summary(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
