"""Offline tests for the deterministic-checks adapter (utils/det_checks.py, judge v13) and
the scoring layer it feeds (judge._apply_harness_verdicts / _finalize_case).

Run from judge/:  python tests_offline/test_det_checks.py
or  uv run pytest tests_offline/test_det_checks.py -q
(it builds several small workbooks: on the shared 16 GB grading Mac, run it through the
heavy-job guard like any multi-workbook job).

No DB, S3, LLM, LibreOffice or Excel: workbooks are built with openpyxl in temporary
folders, and the one LibreOffice call the recalculation pipeline would make for an
openpyxl-written file is replaced by a fake that copies the file (the toys hold constants
only, so the copy carries the same values) and records the policy it was given.
"""
import contextlib
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))

import openpyxl  # noqa: E402

from utils.misc_utils import load_project_configs, project_prefix  # noqa: E402

load_project_configs()

import importlib.util  # noqa: E402

import detchecks.api as det_api  # noqa: E402
import detchecks.core.recalc as det_recalc  # noqa: E402
from detchecks.checks import REGISTRY  # noqa: E402
from detchecks.core.sheet import ExcelError  # noqa: E402
from utils import det_checks as D  # noqa: E402
from utils import rubric_suitability, workbook_properties  # noqa: E402

_spec = importlib.util.spec_from_file_location("_judge_module", str(JUDGE / "main_scripts" / "judge.py"))
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)

RUBRIC_PATH = JUDGE / "prompts" / "rubrics" / "rubric_9.json"
WEIGHTS_PATH = JUDGE / "prompts" / "rubrics" / "rubric_9_weights.json"
RUBRIC = json.loads(RUBRIC_PATH.read_text())
WEIGHTS = json.loads(WEIGHTS_PATH.read_text())
FLAT = [(cat, c["name"]) for cat, checks in RUBRIC.items() for c in checks]

# The maintainer's decisions (2026-10-04) — the config must say exactly this.
LIVE = {29, 47, 49, 50, 51, 61, 62, 66, 69, 70, 73, 74, 77, 80, 87, 92, 93, 94, 95}
RECORDED_ONLY = {22, 65}


def key(no):
    return "/".join(D.DET_CHECK_NAMES[no])


# --------------------------------------------------------------------------- fixtures
@contextlib.contextmanager
def env(**overrides):
    """Temporarily set BIZBENCHJUDGE_<KEY> config values (None removes one)."""
    prefix = project_prefix()
    saved = {}
    try:
        for k, v in overrides.items():
            name = f"{prefix}_{k}"
            saved[name] = os.environ.get(name)
            if v is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(v)
        yield
    finally:
        for name, v in saved.items():
            if v is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = v


@contextlib.contextmanager
def fake_libreoffice():
    """Replace detchecks' LibreOffice runner: copy the file, record (src, out_dir, policy)."""
    calls = []
    original = det_recalc.libreoffice_recalc

    def _fake(src, out_dir, policy):
        calls.append({"src": src, "out_dir": out_dir, "policy": policy})
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + ".xlsx")
        shutil.copy(src, out)
        return out

    det_recalc.libreoffice_recalc = _fake
    try:
        yield calls
    finally:
        det_recalc.libreoffice_recalc = original


@contextlib.contextmanager
def patched_grade(fn):
    original = det_api.grade
    det_api.grade = fn
    try:
        yield
    finally:
        det_api.grade = original


def make_task(root: Path, *, hidden=False, negative=False, origin="Model_v3.xlsx", corrupt=False) -> Path:
    """A staged task folder: ai_attempt.xlsx (+ _attempt_origin.json unless origin is None)."""
    folder = Path(tempfile.mkdtemp(dir=root))
    if corrupt:
        (folder / "ai_attempt.xlsx").write_bytes(b"PK\x03\x04 this is not a workbook")
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Model"
        ws["A1"], ws["B1"] = "Revenue", 100
        ws["A2"], ws["B2"] = "Cost", (-5 if negative else 5)
        notes = wb.create_sheet("Notes")
        notes["A1"] = "source: case pack"
        if hidden:
            notes.sheet_state = "hidden"
        wb.save(folder / "ai_attempt.xlsx")
    if origin is not None:
        (folder / workbook_properties.ORIGIN_FILENAME).write_text(
            json.dumps({"original_filename": origin, "source": "test", "attempt_id": 0}))
    return folder


def stage_annotation(folder: Path, not_applicable=()) -> Path:
    """A complete 'julian' rubric-suitability annotation with the given numbers not_applicable."""
    rubrics = [{"no": i, "category": cat, "name": name,
                "verdict": "not_applicable" if i in not_applicable else "applicable", "conditional": False}
               for i, (cat, name) in enumerate(FLAT, 1)]
    doc = {"annotator": "julian", "complete": True, "created_at": "2026-10-04", "rubric_version": "test",
           "rubrics": rubrics, "_staging": {"s3_key": "test/annotation.json"}}
    p = folder / rubric_suitability.STAGED_FILENAME
    p.write_text(json.dumps(doc))
    return p


def run(folder, **kw):
    kw.setdefault("benchmark", None)
    return D.run_det_checks(folder, rubric_path=RUBRIC_PATH, weights_path=WEIGHTS_PATH, **kw)


def strict_json(text):
    def _bad(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=_bad)


# --------------------------------------------------------------------------- config / pins
def test_pins_registry_rubric_weights_and_config():
    for no, cls in REGISTRY.items():
        assert no in D.DET_CHECK_NAMES, f"registered check {no} has no pin"
        assert cls.key == key(no), (no, cls.key, D.DET_CHECK_NAMES[no])
    for no, pin in D.DET_CHECK_NAMES.items():
        assert FLAT[no - 1] == pin, f"pin {no} {pin} != rubric_9 {FLAT[no - 1]}"
        cat, name = pin
        assert any(e["name"] == name for e in WEIGHTS[cat]), f"{pin} not weighted"
    s = D.load_settings()
    assert s.enabled is True, "det_checks.enabled must be true for judge v13"
    assert set(s.live) == LIVE, sorted(set(s.live) ^ LIVE)
    assert set(s.recorded_only) == RECORDED_ONLY
    assert s.excel_recalc is False, "Excel recalculation must stay OFF (maintainer 2026-10-04)"
    assert s.libreoffice_path == os.environ[f"{project_prefix()}_PATHS_LIBREOFFICE_PATH"]
    assert s.libreoffice_timeout_s == 600
    assert s.libreoffice_max_mb == 10, "LibreOffice size limit is 10 MB (maintainer 2026-10-04)"
    plan = D.configured_checks(s, RUBRIC)
    assert {n for n, live in plan.items() if live} == LIVE
    assert {n for n, live in plan.items() if not live} == RECORDED_ONLY
    assert D.label(92) == "No hidden sheets (92)" and D.label_for_key(key(77)) == "File extension (.xlsx) (77)"


def test_config_errors_refuse_to_grade():
    s = D.load_settings()
    cases = {
        "overlap": s.__class__(**{**s.__dict__, "live": (92,), "recorded_only": (92,)}),
        "unpinned": s.__class__(**{**s.__dict__, "live": (23,), "recorded_only": ()}),
        "not live in detchecks": s.__class__(**{**s.__dict__, "live": (65,), "recorded_only": ()}),
    }
    for what, bad in cases.items():
        try:
            D.configured_checks(bad, RUBRIC)
        except D.DetChecksConfigError as e:
            assert "(" in str(e), e
        else:
            raise AssertionError(f"{what}: no DetChecksConfigError")
    drifted = copy.deepcopy(RUBRIC)
    pd = drifted["Potential Dangers"]
    i = next(i for i, c in enumerate(pd) if c["name"] == "No hidden sheets")
    pd[i], pd[i + 1] = pd[i + 1], pd[i]                  # 92 and 93 swap places
    try:
        D.configured_checks(s, drifted)
    except D.DetChecksConfigError as e:
        assert "numbering drift" in str(e)
    else:
        raise AssertionError("rubric drift not refused")
    small = {"Accuracy": RUBRIC["Accuracy"][:5]}         # a v1-sized rubric: none of the checks exists
    assert D.configured_checks(s, small) == {}
    for bad_env in ({"DET_CHECKS_LIVE": "92,x"}, {"DET_CHECKS_LIVE": "92,92"},
                    {"DET_CHECKS_LIBREOFFICE_MAX_MB": "0"}):
        with env(**bad_env):
            try:
                D.load_settings()
            except D.DetChecksConfigError:
                pass
            else:
                raise AssertionError(f"bad config accepted: {bad_env}")


def test_startup_check():
    assert D.startup_check(RUBRIC_PATH) == "harness"
    assert D.startup_check(RUBRIC_PATH, "llm") == "llm"
    assert D.startup_check("/nonexistent/rubric.json", "off") == "off"       # off reads nothing
    with env(DET_CHECKS_LIVE="29,65"):
        try:
            D.startup_check(RUBRIC_PATH)
        except D.DetChecksConfigError as e:
            assert "Negatives in parentheses (65)" in str(e)
        else:
            raise AssertionError("a bad config passed the startup check")


def test_resolve_mode():
    s = D.load_settings()
    assert D.resolve_mode(None, s) == "harness"
    assert D.resolve_mode("llm", s) == "llm" and D.resolve_mode("off", s) == "off"
    assert D.resolve_mode(None, s.__class__(**{**s.__dict__, "enabled": False})) == "off"
    try:
        D.resolve_mode("shadow", s)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown mode accepted")


# --------------------------------------------------------------------------- verdicts
def test_live_fail_and_live_pass():
    with tempfile.TemporaryDirectory() as tmp, fake_libreoffice():
        root = Path(tmp)
        bad = run(make_task(root, hidden=True))
        good = run(make_task(root, hidden=False))
    e = bad.harness_verdicts[key(92)]
    assert e["engine"] == "harness" and e["live"] is True and e["family"] == "det_checks"
    assert e["decision"] == "fail" and e["check_no"] == 92 and e["fallback_reason"] is None
    assert e["n_mistakes"] == 1 and len(e["mistakes"]) == 1 and "Notes" in e["mistakes"][0]["location"]
    assert isinstance(e["stats"], dict) and e["summary"]
    g = good.harness_verdicts[key(92)]
    assert g["engine"] == "harness" and g["decision"] == "pass" and g["mistakes"] == [] and g["n_mistakes"] == 0
    # every configured check was graded (no annotation staged, benchmark None: retired rule only)
    assert set(good.summary["graded"]) == LIVE | RECORDED_ONLY and good.summary["not_applicable"] == []
    assert good.summary["checks"][key(92)] == {"check_no": 92, "engine": "harness", "decision": "pass",
                                              "live": True, "n_mistakes": 0}
    assert bad.mode == "harness" and bad.for_judge()["mode"] == "harness"


def test_recorded_only_check_never_overlays():
    with tempfile.TemporaryDirectory() as tmp, fake_libreoffice():
        root = Path(tmp)
        r = run(make_task(root, negative=True))
        e = r.harness_verdicts[key(65)]
        assert e["engine"] == "llm" and e["live"] is False and e["decision"] == "fail"
        assert e["fallback_reason"] == det_api.LIVE_NOTE and e["n_mistakes"] == 1
        assert r.harness_verdicts[key(22)]["engine"] == "llm"           # the other v3-bucket check
        # the config can also hold a live-capable check back: 92 recorded only
        with env(DET_CHECKS_LIVE="29,47", DET_CHECKS_RECORDED_ONLY="22,65,92"):
            r2 = run(make_task(root, hidden=True))
        e2 = r2.harness_verdicts[key(92)]
        assert e2["engine"] == "llm" and e2["live"] is False and e2["decision"] == "fail"
        assert set(r2.summary["graded"]) == {22, 29, 47, 65, 92}


def test_suitability_gate_matches_the_judge():
    with tempfile.TemporaryDirectory() as tmp, fake_libreoffice():
        root = Path(tmp)
        folder = make_task(root, hidden=True)
        stage_annotation(folder, not_applicable={51, 92, 65})
        r = run(folder, benchmark="v2")
        assert key(92) not in r.harness_verdicts and key(51) not in r.harness_verdicts
        assert set(r.summary["not_applicable"]) == {51, 65, 92}
        # the judge's own gate on the same folder (single_pass_judge_case: load_for_case +
        # build_effective_weights) scores exactly the pinned checks the adapter graded
        suit = rubric_suitability.load_for_case(folder, RUBRIC, "v2")
        eff = rubric_suitability.build_effective_weights(WEIGHTS, suit["excluded"])
        judged = {(cat, e["name"]) for cat, es in eff.items() if cat != "CategoryWeights" for e in es}
        assert {n for n, pin in D.DET_CHECK_NAMES.items() if pin in judged} == set(r.summary["graded"])
        # every configured check gated out: nothing runs, so even an unreadable file passes through
        dead = make_task(root, corrupt=True)
        stage_annotation(dead, not_applicable=LIVE | RECORDED_ONLY)
        r0 = run(dead, benchmark="v2")
        assert r0.harness_verdicts == {} and r0.summary["status"] == "no_applicable_checks"
        assert strict_json((dead / D.ARTEFACT_FILENAME).read_text())["status"] == "no_applicable_checks"
        # v2 without an annotation refuses exactly like the judge does
        try:
            run(make_task(root), benchmark="v2")
        except rubric_suitability.SuitabilityError:
            pass
        else:
            raise AssertionError("v2 grading without an annotation was not refused")


def test_task_meta_file_extension():
    with tempfile.TemporaryDirectory() as tmp, fake_libreoffice():
        root = Path(tmp)
        folder = make_task(root, origin="Deliverable.xls")
        r = run(folder)
        e = r.harness_verdicts[key(77)]
        assert e["engine"] == "harness" and e["decision"] == "fail"
        assert e["stats"]["delivered_filename"] == "Deliverable.xls"
        assert e["stats"]["filename_source"] == "task_meta.delivered_filename"
        art = strict_json((folder / D.ARTEFACT_FILENAME).read_text())
        assert art["task_meta"] == {"requires_external_links": False, "delivered_filename": "Deliverable.xls"}
        assert r.summary["delivered_filename"] == "Deliverable.xls"
        ok = run(make_task(root, origin="Model.XLSX"))
        assert ok.harness_verdicts[key(77)]["decision"] == "pass"
        # no sidecar: File extension (.xlsx) (77) raises, loudly, naming the check and the file
        lost = make_task(root, origin=None)
        try:
            run(lost)
        except D.DetChecksError as err:
            msg = str(err)
            assert "File extension (.xlsx) (77)" in msg and str(lost / "ai_attempt.xlsx") in msg
            assert workbook_properties.ORIGIN_FILENAME in msg and "before the LLM call" in msg
            assert set(err.failures) == {key(77)} and isinstance(err, det_api.GradingError)
        else:
            raise AssertionError("missing delivered filename did not raise")
        art = strict_json((lost / D.ARTEFACT_FILENAME).read_text())
        assert art["status"] == "error" and key(77) in art["failures"]
        assert art["task_meta"] == {"requires_external_links": False}


def test_recalc_reads_the_delivered_file_with_the_configured_policy():
    with tempfile.TemporaryDirectory() as tmp, fake_libreoffice() as calls:
        folder = make_task(Path(tmp), negative=True)
        (folder / "temp_recalculated").mkdir()
        shutil.copy(folder / "ai_attempt.xlsx", folder / "temp_recalculated" / "ai_attempt.xlsx")
        run(folder)
        assert len(calls) == 1, calls
        c = calls[0]
        assert Path(c["src"]) == folder / "ai_attempt.xlsx"            # never temp_recalculated/
        p = c["policy"]
        assert p.libreoffice_path == os.environ[f"{project_prefix()}_PATHS_LIBREOFFICE_PATH"]
        assert p.excel_allowed is False and p.lo_timeout_s == 600
        assert Path(p.workdir) == folder / D.RECALC_DIRNAME
        assert Path(c["out_dir"]).is_relative_to(folder / D.RECALC_DIRNAME)


def test_missing_libreoffice_fails_loudly_without_launching_anything():
    with tempfile.TemporaryDirectory() as tmp:
        folder = make_task(Path(tmp), negative=True)
        with env(PATHS_LIBREOFFICE_PATH="/nonexistent/LibreOffice/soffice"):
            try:
                run(folder)
            except D.DetChecksError as err:
                msg = str(err)
                for no in (65, 66, 73, 94):                            # value checks
                    assert D.label(no) in msg, (no, msg)
                assert "LibreOffice not found" in msg and str(folder / "ai_attempt.xlsx") in msg
                assert "paths.libreoffice_path" in msg
                assert key(92) not in err.failures                     # structure checks were fine
            else:
                raise AssertionError("missing LibreOffice did not raise")
        assert strict_json((folder / D.ARTEFACT_FILENAME).read_text())["status"] == "error"


def test_libreoffice_size_limit_fails_loudly_without_running_it():
    with tempfile.TemporaryDirectory() as tmp, fake_libreoffice() as calls:
        folder = make_task(Path(tmp), negative=True)
        size = (folder / "ai_attempt.xlsx").stat().st_size
        with env(DET_CHECKS_LIBREOFFICE_MAX_MB=str(size / 2 / 1_000_000)):
            try:
                run(folder)
            except D.DetChecksError as err:
                msg = str(err)
                assert "not graded: too large" in msg and "det_checks.libreoffice_max_mb" in msg
                assert D.label(65) in msg and D.label(66) in msg and str(folder / "ai_attempt.xlsx") in msg
                assert key(92) not in err.failures and key(77) not in err.failures
            else:
                raise AssertionError("a file over the LibreOffice size limit was graded")
        assert calls == [], "LibreOffice must not run on a file over the limit"
        # under the limit the same file goes to LibreOffice as usual
        with env(DET_CHECKS_LIBREOFFICE_MAX_MB=str(size * 2 / 1_000_000)):
            run(folder)
        assert len(calls) == 1


def test_mode_off_runs_nothing():
    def _boom(*a, **k):
        raise AssertionError("detchecks must not run in mode off")
    with tempfile.TemporaryDirectory() as tmp, patched_grade(_boom):
        folder = make_task(Path(tmp), corrupt=True)
        r = run(folder, mode="off")
        assert r.mode == "off" and r.harness_verdicts == {} and r.summary == {"status": "off", "mode": "off"}
        assert strict_json((folder / D.ARTEFACT_FILENAME).read_text())["status"] == "off"
        with env(DET_CHECKS_ENABLED="false"):
            assert run(folder).mode == "off"


def test_mode_llm_records_the_same_entries():
    with tempfile.TemporaryDirectory() as tmp, fake_libreoffice():
        folder = make_task(Path(tmp), hidden=True)
        h = run(folder, mode="harness")
        s = run(folder, mode="llm")
    assert s.mode == "llm" and s.for_judge()["mode"] == "llm"
    strip = lambda hv: {k: {kk: vv for kk, vv in v.items() if kk != "stats"} for k, v in hv.items()}  # noqa: E731
    assert strip(s.harness_verdicts) == strip(h.harness_verdicts)   # the switch acts at scoring


def test_json_safety():
    nasty = {
        "nan": float("nan"), "inf": float("inf"), ("Sheet", 3): {1, 2}, 7: frozenset({"b", "a"}),
        "err": ExcelError("#N/A"), "path": Path("/x/y.xlsx"), "tuple": (1, 2.5, None),
        "bytes": b"\xffabc", "mixed_set": {1, "a"}, "nested": [{"v": -float("inf")}],
    }
    text = json.dumps(D.json_safe(nasty), allow_nan=False)
    back = strict_json(text)
    assert back["nan"] == "nan" and back["('Sheet', 3)"] == [1, 2] and back["7"] == ["a", "b"]
    assert back["err"] == "#N/A" and type(D.json_safe(ExcelError("#N/A"))) is str

    def _crafted(path, *, checks, task_meta, recalc):
        return {key(92): {"engine": "harness", "decision": "fail", "summary": "s",
                          "mistakes": [{"location": "'Notes'", "description": "d", "severity": "major"}],
                          "stats": {"n_mistakes": 1, "ratio": float("nan"), ("a", 1): {3, 1},
                                    "err": ExcelError("#REF!"), "where": Path(path)},
                          "live": True}}
    with tempfile.TemporaryDirectory() as tmp, patched_grade(_crafted), env(DET_CHECKS_LIVE="92", DET_CHECKS_RECORDED_ONLY=""):
        folder = make_task(Path(tmp))
        r = run(folder)
        json.dumps(r.harness_verdicts, allow_nan=False)
        json.dumps(r.summary, allow_nan=False)
        json.dumps(r.for_judge(), allow_nan=False)
        st = r.harness_verdicts[key(92)]["stats"]
        assert st["ratio"] == "nan" and st["('a', 1)"] == [1, 3] and st["err"] == "#REF!"
        strict_json((folder / D.ARTEFACT_FILENAME).read_text())


def test_merge_harness_verdicts():
    a = {"Accuracy/Final calculation accuracy": {"engine": "harness"}}
    b = {key(92): {"engine": "harness"}}
    merged = D.merge_harness_verdicts(a, b)
    assert set(merged) == set(a) | set(b) and D.merge_harness_verdicts(None, None) == {}
    try:
        D.merge_harness_verdicts(a, {"Accuracy/Final calculation accuracy": {}})
    except ValueError:
        pass
    else:
        raise AssertionError("overlapping keys accepted")


def test_reaper_kills_only_this_gradings_libreoffice():
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / D.RECALC_DIRNAME
        profile = workdir / "_lo_profiles" / "lo_test"
        profile.mkdir(parents=True)
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        ours = subprocess.Popen(sleeper + [f"-env:UserInstallation=file://{profile}"], start_new_session=True)
        other = subprocess.Popen(sleeper + [f"-env:UserInstallation=file://{Path(tmp) / 'elsewhere'}"],
                                 start_new_session=True)
        try:
            time.sleep(0.3)
            killed = D._reap_libreoffice(workdir)
            assert ours.pid in killed and other.pid not in killed, killed
            assert ours.wait(timeout=10) != 0
            assert other.poll() is None, "an unrelated process was killed"
        finally:
            for p in (ours, other):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=10)


# --------------------------------------------------------------------------- scoring layer
def _llm_judgement(fail=(), skip=()):
    """An LLM judgement over all of rubric_9: pass everywhere except `fail` (by number);
    numbers in `skip` are not recorded at all."""
    out = {}
    for no, (cat, name) in enumerate(FLAT, 1):
        if no in skip:
            continue
        failing = no in fail
        out.setdefault(cat, []).append({
            "check": str(no), "name": name, "decision": "fail" if failing else "pass", "summary": "llm",
            "mistakes": [{"location": "S!A1", "description": "llm", "severity": "major"}] if failing else [],
        })
    return out


def _det_entry(no, decision, live=True):
    mistakes = [{"location": "'Notes'", "description": "py", "severity": "major"}] if decision == "fail" else []
    return {"engine": "harness" if live else "llm", "decision": decision, "summary": f"py {no}",
            "mistakes": mistakes, "fallback_reason": None if live else det_api.LIVE_NOTE,
            "family": "det_checks", "live": live, "check_no": no, "n_mistakes": len(mistakes),
            "stats": {"n_mistakes": len(mistakes), "values": {"source": "cache"}}}


FA = "Accuracy/Final calculation accuracy"
HARNESS = {
    FA: {"engine": "harness", "decision": "pass", "summary": "75/75", "mistakes": [],
         "fallback_reason": None, "n_questions": 75, "n_match": 75},
    key(92): _det_entry(92, "pass"),                  # LLM fails it below
    key(61): _det_entry(61, "fail"),                  # LLM passes it
    key(74): _det_entry(74, "fail"),                  # LLM never recorded it
    key(65): _det_entry(65, "fail", live=False),      # recorded only; LLM passes
    key(22): _det_entry(22, "pass", live=False),      # recorded only; LLM fails
}
LLM_FAIL = {1, 92, 22}


def test_apply_harness_verdicts_with_deterministic_entries():
    llm = _llm_judgement(fail=LLM_FAIL, skip={74})
    overlaid, prov = judge._apply_harness_verdicts(llm, HARNESS, WEIGHTS)
    p92 = prov[key(92)]
    assert p92["engine"] == "harness" and p92["agreed"] is False and p92["llm_decision"] == "fail"
    assert p92["family"] == "det_checks" and p92["live"] is True and p92["check_no"] == 92
    assert p92["n_mistakes"] == 0 and p92["stats"]["values"]["source"] == "cache"
    item92 = next(i for i in overlaid["Potential Dangers"] if i["name"] == "No hidden sheets")
    assert item92["decided_by"] == "harness" and item92["decision"] == "pass" and item92["llm_decision"] == "fail"
    # recorded only: Python decision + agreement recorded, the LLM item untouched
    p65 = prov[key(65)]
    assert p65["engine"] == "llm" and p65["decision"] == "fail" and p65["llm_decision"] == "pass"
    assert p65["agreed"] is False and p65["live"] is False and p65["fallback_reason"] == det_api.LIVE_NOTE
    item65 = next(i for i in overlaid["Formatting"] if i["name"] == "Negatives in parentheses")
    assert item65["decision"] == "pass" and "decided_by" not in item65
    assert prov[key(22)]["agreed"] is False and prov[key(61)]["agreed"] is False
    # a check the LLM never recorded is inserted under its rubric number
    item74 = next(i for i in overlaid["Formatting"] if i["name"] == "No merged cells")
    assert item74["check"] == "74" and item74["decided_by"] == "harness" and item74["llm_decision"] is None
    assert prov[key(74)]["agreed"] is None
    # the answer-check entry keeps its v12 provenance
    assert prov[FA]["engine"] == "harness" and prov[FA]["n_match"] == 75 and "family" not in prov[FA]


def _finalize(llm, hv, accuracy_engine, det_checks):
    out = Path(tempfile.mkdtemp(prefix="detchecks_finalize_"))
    log = out / "cache.log"
    log.write_text("")
    tt = {"evaluations": {}, "total_message_size": 0, "total_message_size_with_images": 0, "total_tokens": 0,
          "total_prompt_tokens": 0, "total_completion_tokens": 0, "total_cost": 0.0}
    res = judge._finalize_case(
        all_responses=copy.deepcopy(llm), output_dir=out, weights_data=WEIGHTS, token_tracking=tt,
        model="test/model", attempt_model="agent", task_folder_name="t", golden_solution_files={},
        ai_attempt_files={}, context_file_path=None, start_time=time.time(), cache_log_path=str(log),
        versions={"JUDGE_VERSION": "13", "PROMPT_VERSION": "8", "RUBRIC_VERSION": "9", "RUBRIC_WEIGHT_VERSION": "9"},
        agentic=True, harness_verdicts=hv, accuracy_engine=accuracy_engine, det_checks=det_checks,
    )
    return res, out


def test_finalize_case_switches():
    llm = _llm_judgement(fail=LLM_FAIL, skip={74})
    with tempfile.TemporaryDirectory() as tmp:
        artefact = Path(tmp) / "det_checks.json"
        artefact.write_text(json.dumps({"status": "ok", "verdicts": {}}))
        summary = {"status": "ok", "mode": None, "graded": [22, 61, 65, 74, 92], "not_applicable": [],
                   "checks": {}, "code_sha": "x", "values": {"source": "cache"}}
        results = {}
        for acc in ("harness", "llm"):
            for det in ("harness", "llm"):
                dc = {"mode": det, "summary": {**summary, "mode": det}, "artefact": str(artefact)}
                results[(acc, det)] = _finalize(llm, HARNESS, acc, dc)
        expected = {("harness", "harness"): "harness", ("harness", "llm"): "mixed",
                    ("llm", "harness"): "mixed", ("llm", "llm"): "llm"}
        totals_llm, totals_h = set(), set()
        for (acc, det), (res, out) in results.items():
            sr = res["score_results"]
            eng = sr["accuracy_engine"]
            assert eng["effective"] == expected[(acc, det)], (acc, det, eng["effective"])
            assert eng["mode"] == acc and eng["det_checks_mode"] == det
            totals_llm.add(eng["total_score_llm"])
            totals_h.add(eng["total_score_harness"])
            counted = {k for k, v in eng["checks"].items() if v["counted"]}
            want = ({FA} if acc == "harness" else set()) | ({key(92), key(61), key(74)} if det == "harness" else set())
            assert counted == want, (acc, det, counted)
            assert not eng["checks"][key(65)]["counted"] and not eng["checks"][key(22)]["counted"]
            # recorded-only checks always score the LLM's verdict
            assert sr["check_scores"]["Formatting"]["Negatives in parentheses"]["score"] == 1.0
            assert sr["check_scores"]["Error Checks"]["No formula errors"]["score"] == 0.0
            # live checks follow Python only when det_checks counts
            hid = sr["check_scores"]["Potential Dangers"]["No hidden sheets"]["score"]
            assert hid == (1.0 if det == "harness" else 0.0), (acc, det, hid)
            assert sr["det_checks"]["mode"] == det and (out / "det_checks.json").exists()
            meta = json.loads((out / "_metadata.json").read_text())
            assert meta["det_checks"]["mode"] == det and meta["det_checks"]["graded"] == summary["graded"]
            strict_json((out / "scores.json").read_text())
            json.dumps(sr, allow_nan=False)
            assert "decided_by" not in json.dumps(json.loads((out / "ai_judgement.json").read_text()))
            if expected[(acc, det)] == "harness":
                assert sr["total_score"] == eng["total_score_harness"]
            if expected[(acc, det)] == "llm":
                assert sr["total_score"] == eng["total_score_llm"]
        assert len(totals_llm) == 1 and len(totals_h) == 1, "both totals are the same under every switch"
        # mixed: exactly the counted family overlaid
        res, _ = results[("llm", "harness")]
        only_det = {k: v for k, v in HARNESS.items() if k != FA}
        overlaid, _ = judge._apply_harness_verdicts(llm, only_det, WEIGHTS)
        want_total = judge.calculate_scores(overlaid, WEIGHTS, max_mistakes=judge.RUBRIC_MAX_MISTAKES)["total_score"]
        assert abs(res["score_results"]["total_score"] - want_total) < 1e-9
        assert res["final_score"] == res["score_results"]["total_score"]


def test_finalize_case_without_det_checks_keeps_the_v12_shape():
    llm = _llm_judgement(fail={1})
    res, out = _finalize(llm, {FA: HARNESS[FA]}, "harness", None)
    sr = res["score_results"]
    assert "det_checks" not in sr and "det_checks_mode" not in sr["accuracy_engine"]
    assert sr["accuracy_engine"]["effective"] == "harness" and not (out / "det_checks.json").exists()
    assert "det_checks" not in json.loads((out / "_metadata.json").read_text())


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
