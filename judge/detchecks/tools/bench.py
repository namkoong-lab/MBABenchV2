"""Performance probe for one workbook (wrap in /usr/bin/time -l for RSS).

    cd judge && /usr/bin/time -l python -m detchecks.tools.bench <path> [--full]

1. grade(path) with every registered check (what production would run today).
2. --full: a synthetic check that streams EVERY row and cell of every worksheet and touches
   what a cell-level check typically reads (value + trust, expanded formula text, array
   membership, font colour, fill colour, number format, rich runs).  This bounds the cost of
   future cell-level checks on the same file.
Prints seconds and the process's peak RSS.
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time

from ..api import Engine, grade
from ..checks.base import Check


class TouchAll(Check):
    number = 0
    key = "Bench/touch every cell"
    needs_cells = True
    needs_rows = True
    needs_values = True             # reads cell.value of trusted formula results

    def start(self, wb):
        super().start(wb)
        self.st = wb.styles
        self.n_rows = self.n_cells = self.n_formulas = self.n_shared_children = 0
        self.n_array_members = self.n_trusted = self.n_rich = 0
        self.fills = set()
        self.fonts = set()

    def row(self, row):
        self.n_rows += 1

    def cell(self, cell):
        self.n_cells += 1
        if cell.has_formula:
            self.n_formulas += 1
            if cell.formula.is_shared_child:
                self.n_shared_children += 1
            cell.formula_text
        if cell.array is not None:
            self.n_array_members += 1
        if cell.value_trusted:
            self.n_trusted += 1
            cell.value
        self.fonts.add(self.st.font_color(cell.s))
        self.fills.add(self.st.fill_color(cell.s))
        self.st.num_fmt_code(cell.s)
        if cell.rich_runs:
            self.n_rich += 1

    def finish(self):
        self.stats = {"rows": self.n_rows, "cells": self.n_cells, "formulas": self.n_formulas,
                      "shared_children_expanded": self.n_shared_children, "array_members": self.n_array_members,
                      "trusted_values": self.n_trusted, "rich_cells": self.n_rich,
                      "distinct_font_colours": len(self.fonts), "distinct_fill_colours": len(self.fills)}
        return self.verdict("touched", "never")


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(argv)
    out = {"path": args.path}
    t0 = time.perf_counter()
    v = grade(args.path)
    out["registered_checks"] = {k: x["decision"] for k, x in v.items()}
    out["registered_seconds"] = round(time.perf_counter() - t0, 2)
    out["rss_after_registered_mb"] = round(peak_rss_mb(), 1)
    if args.full:
        t0 = time.perf_counter()
        chk = TouchAll()
        eng = Engine(args.path, [chk])
        eng.run()
        out["full_stream_seconds"] = round(time.perf_counter() - t0, 2)
        out["full_stream_stats"] = chk.stats
        out["per_sheet_seconds"] = sorted(((s["sheet"], s["s"]) for s in eng.timings.get("sheets", [])),
                                          key=lambda x: -x[1])[:8]
    out["peak_rss_mb"] = round(peak_rss_mb(), 1)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
