"""Offline tests for the judge v13 reporting fixes: report_accuracy_engine prints the recorded
total and explains a "mixed" row (one verdict family counted, the other did not).

Run from judge/:  python tests_offline/test_det_checks_reports.py   (or pytest)
No DB, S3, LLM or LibreOffice: scored_results come from judge._finalize_case on a stub judgement
(test_det_checks' fixtures); the DB connection, the LLM client and the grading are stubs.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))
sys.path.insert(0, str(JUDGE / "tests_offline"))

import test_det_checks as T  # noqa: E402  (loads the project config, judge.py and the fixtures)

D = T.D


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(JUDGE / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RAE = _load("_report_accuracy_engine", "operation_scripts/report_accuracy_engine.py")


def _scored(acc, det, det_summary_checks=None):
    """scored_results of a real _finalize_case run under the two switches."""
    llm = T._llm_judgement(fail=T.LLM_FAIL, skip={74})
    summary = {"status": "ok", "mode": det, "graded": [22, 61, 65, 74, 92], "not_applicable": [],
               "checks": det_summary_checks or {}, "code_sha": "x", "values": {"source": "cache"}}
    with tempfile.TemporaryDirectory() as tmp:
        artefact = Path(tmp) / "det_checks.json"
        artefact.write_text("{}")
        res, _out = T._finalize(llm, T.HARNESS, acc, {"mode": det, "summary": summary, "artefact": str(artefact)})
    return res["score_results"]


def test_report_accuracy_engine_prints_the_recorded_total_and_explains_mixed():
    rows = {}
    for acc in ("harness", "llm"):
        for det in ("harness", "llm"):
            sr = _scored(acc, det)
            rows[(acc, det)] = RAE._row_from_scored(1, 2, "g", None, sr, task_id=3)
            assert rows[(acc, det)]["total"] == sr["total_score"]
    assert rows[("harness", "harness")]["in_db"] == "harness"
    assert rows[("llm", "llm")]["in_db"] == "llm"
    assert rows[("harness", "llm")]["in_db"] == "mixed: answer check harness, det checks llm"
    assert rows[("llm", "harness")]["in_db"] == "mixed: answer check llm, det checks harness"
    mixed = rows[("llm", "harness")]
    assert mixed["total"] not in (mixed["total_llm"], mixed["total_harness"]), mixed
    assert rows[("harness", "harness")]["total"] == rows[("harness", "harness")]["total_harness"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        RAE._print(list(rows.values()))
    text = buf.getvalue()
    assert "recorded total" in text.splitlines()[0] and "in DB" in text.splitlines()[0]
    assert "mixed: answer check llm, det checks harness" in text and RAE.LEGEND in text
    assert f"{mixed['total']:.2f}" in text
    local = RAE._row_from_scored("run", 2, "g", None, _scored("llm", "harness"), in_db=False)
    assert local["in_db"] == "not written (mixed: answer check llm, det checks harness)"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"OK   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
