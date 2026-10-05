"""Offline tests for the judge v13 reporting fixes: report_accuracy_engine (recorded total, "mixed"
explained), report_toy_reliability --show-misses (Python's summary when Python decided), and
grade_toy recording the det-checks mode that actually ran in toy_runs.

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
RTR = _load("_report_toy_reliability", "operation_scripts/report_toy_reliability.py")
K92 = T.key(92)


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


def _toy_row(sr, check_no, category, name, verdict, expected, llm_summary="the LLM's reasoning"):
    return {"check_no": check_no, "variant": expected, "repeat_no": 1, "verdict": verdict, "expected": expected,
            "category": category, "check_name": name, "llm_summary": llm_summary, "scored_results": json.dumps(sr)}


def test_report_toy_reliability_shows_python_summary_when_python_decided():
    checks = {K92: {"check_no": 92, "engine": "harness", "decision": "pass", "live": True, "n_mistakes": 0,
                    "summary": "No hidden sheets among 3 sheet(s)."}}
    sr_h = _scored("harness", "harness", checks)
    line = RTR.miss_line(_toy_row(sr_h, 92, "Potential Dangers", "No hidden sheets", "pass", "fail"))
    assert "judged pass by Python (the LLM said fail)" in line and "No hidden sheets among 3 sheet(s)." in line
    assert "the LLM's reasoning" not in line
    # matched by rubric number when the toy's category label differs
    line = RTR.miss_line(_toy_row(sr_h, 92, "Dangers", "No hidden sheets", "pass", "fail"))
    assert "by Python" in line and "No hidden sheets among 3 sheet(s)." in line
    # shadow run (--det-checks llm): the LLM's verdict counted -> the LLM's summary
    sr_l = _scored("harness", "llm", checks)
    line = RTR.miss_line(_toy_row(sr_l, 92, "Potential Dangers", "No hidden sheets", "fail", "pass"))
    assert "by Python" not in line and "the LLM's reasoning" in line
    # a check Python does not grade -> the LLM's summary
    line = RTR.miss_line(_toy_row(sr_h, 5, "Accuracy", "Something", "fail", "pass"))
    assert "by Python" not in line and "the LLM's reasoning" in line
    # a Python verdict from a row written before the summary was recorded
    line = RTR.miss_line(_toy_row(_scored("harness", "harness"), 92, "Potential Dangers", "No hidden sheets",
                                  "pass", "fail"))
    assert "by Python" in line and "not recorded in this row" in line
    assert RTR.python_verdict({"scored_results": None, "check_no": 92}) is None


def test_grade_toy_records_the_det_checks_mode_that_ran():
    gt = _load("_grade_toy", "main_scripts/grade_toy.py")
    from utils.logger import remove_log_file
    from utils.misc_utils import load_env_var, project_prefix

    captured: dict = {}

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            captured.setdefault("sql", []).append(a[0][:40])

        def fetchone(self):
            return (1,)

        def fetchall(self):
            return []

    class Conn:
        def cursor(self, *a, **k):
            return Cur()

        def commit(self):
            pass

        def close(self):
            pass

    def _grade(**kw):
        captured.setdefault("graded_with", []).append(kw["det_checks"])
        return {"success": False, "error": "stub grading", "elapsed_seconds": 0, "cost": 0}

    gt.gfd.get_db_connection = lambda: Conn()
    gt.insert_run = lambda conn, run_id, args, *a, **k: captured.setdefault("toy_runs_args", []).append(
        json.loads(json.dumps(vars(args), default=str)))
    gt.insert_grading = lambda *a, **k: 1
    gt.get_client = lambda identity: object()
    gt.gfd.grade_single_attempt = _grade
    prefix = project_prefix()
    environ = dict(os.environ)            # grade_toy.main loads the v2 config and redirects S3: undo it all after
    argv = sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "manifest.json"
        manifest.write_text(json.dumps({"toys": [{"check_no": 92, "folder": "toy092", "files": {
            "Pass": {"name": "p.xlsx", "s3_key": "toys/p.xlsx"}, "Fail": {"name": "f.xlsx", "s3_key": "toys/f.xlsx"}}}]}))
        scratch = Path(tmp) / "scratch"
        load_configs = gt.load_project_configs

        def _configs_then_temp_scratch(*a, **k):
            # main() reloads the v2 config, which resets PATHS_SCRATCH_PATH: keep its toy_runs/<run>
            # folders (run.log, suitability/) in this test's temporary folder, not judge/scratch/
            load_configs(*a, **k)
            os.environ[f"{prefix}_PATHS_SCRATCH_PATH"] = str(scratch)

        gt.load_project_configs = _configs_then_temp_scratch
        toy_runs = JUDGE / "scratch" / "toy_runs"
        before = set(toy_runs.iterdir()) if toy_runs.is_dir() else set()
        try:
            for flag, want in ((None, "harness"), ("llm", "llm")):
                sys.argv = ["grade_toy.py", "--run-label", "t", "--model", load_env_var("JUDGE_DEFAULT_GRADER"),
                            "--manifest", str(manifest), "--checks", "92", "--variants", "fail", "--no-s3-upload"]
                if flag:
                    sys.argv += ["--det-checks", flag]
                gt.main()
                assert captured["toy_runs_args"][-1]["det_checks"] == want, captured["toy_runs_args"][-1]
                assert captured["graded_with"][-1] == want
            assert len(list((scratch / "toy_runs").iterdir())) == 2
            after = set(toy_runs.iterdir()) if toy_runs.is_dir() else set()
            assert after == before, f"grade_toy wrote into judge/scratch/toy_runs: {sorted(after - before)}"
        finally:
            sys.argv = argv
            os.environ.clear()
            os.environ.update(environ)
            for run_log in (scratch / "toy_runs").glob("*/run.log"):
                remove_log_file(str(run_log))


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
