"""Deterministic rubric checks in the judge (judge v13).

`detchecks` (judge/detchecks/, API in detchecks/docs/reader.md) grades rubric_9 checks in
Python from the DELIVERED workbook. This module is the only place the judge calls it:

    run = run_det_checks(task_folder, rubric_path=..., weights_path=..., mode=None)
    harness_verdicts = merge_harness_verdicts(answer_check_verdicts, run.harness_verdicts)
    single_pass_judge_case(..., harness_verdicts=harness_verdicts, det_checks=run.for_judge())

What runs
  project_configs.yaml det_checks.live           the Python verdict replaces the LLM's at scoring
  project_configs.yaml det_checks.recorded_only  the Python verdict is recorded beside the LLM's,
                                                 and the LLM's counts
  Both are rubric numbers. Each number is pinned to its (category, name) in DET_CHECK_NAMES and
  checked against the loaded rubric and the detchecks registry; any mismatch refuses to grade
  (numbering drift must never move a verdict onto the wrong check). A configured live check
  must also be live in detchecks (Check.live). A check is graded only when the task's
  rubric-suitability gate leaves it applicable (retired checks never are) and the effective
  weights score it: the same gate judge._apply_harness_verdicts applies.

Representation (the judge's harness_verdicts format, key "<Category>/<check name>")
  live           engine "harness", decision, summary, mistakes, fallback_reason None,
                 family "det_checks", live True, check_no, n_mistakes, stats
  recorded only  the same with engine "llm", live False and fallback_reason = detchecks'
                 LIVE_NOTE ("recorded only; the LLM verdict stands at scoring")
  `engine` keeps its one meaning for every reader: "harness" = this verdict may replace the
  LLM's; "llm" = the LLM's verdict stands (here with the Python decision recorded beside it).
  judge._apply_harness_verdicts records decision, llm_decision and `agreed` for both kinds and
  overlays engine "harness" entries only.

The switch (--det-checks on every driver; default from det_checks.enabled: true -> harness)
  harness  live Python verdicts count in the recorded total (judge v13 default)
  llm      everything runs and is recorded; the LLM's verdicts count (shadow run;
           scored_results.accuracy_engine.total_score_harness still shows the v13 total)
  off      nothing runs; scored_results.det_checks says so
  --accuracy-check keeps deciding the answer check (Final calculation accuracy) on its own.

No fallback (maintainer, 2026-10-02)
  A check that cannot grade the file raises detchecks' GradingError; run_det_checks writes the
  failure to det_checks.json and raises DetChecksError naming every failing check by title and
  the file. Every driver calls it FIRST - before the answer check (which recalculates through
  LibreOffice too, so a file refused here never reaches it) and the LLM - so the attempt fails
  the way the formula-cache refusal does (grade_from_db logs FAILED, no DB row, the batch
  continues) with no API spend.
  Nothing here ever turns a failure into an LLM verdict. This holds in `llm` mode too: when the
  checks run, they run loudly.

Values (detchecks/docs/recalc.md)
  Structure and styles always come from task_folder/ai_attempt.xlsx, the delivered file (never
  temp_recalculated/). Formula values: an Excel-saved file's own caches (any size); any other
  writer's file is recalculated by LibreOffice (paths.libreoffice_path) into task_folder/
  det_checks_recalc/ (the copy and the private profiles), which run_det_checks deletes itself
  as soon as the checks return or raise, in every driver (_remove_recalc_dir; det_checks.json
  stays). Excel recalculation is OFF (det_checks.excel_recalc); cells LibreOffice
  cannot compute are used as displayed. LibreOffice never runs on a file larger than
  det_checks.libreoffice_max_mb (10 MB; maintainer 2026-10-04, memory): such a file fails the
  value checks, so the grading stops before the LLM call ("not graded: too large").
  LibreOffice never outlives the grading (detchecks/docs/recalc.md, "The LibreOffice process"):
  its paths are encoded file URLs (a task folder with a space or '%'); it runs in the grader's
  process group under a watchdog that kills it when the grader dies (SIGKILL included); a
  SIGTERM / SIGHUP handler, installed by startup_check and run_det_checks from the main thread,
  kills it before the grader dies of the signal; and _reap_libreoffice sweeps this grading's
  private profiles (raw or URL form) when the checks return or raise.

Task metadata
  delivered_filename       from the _attempt_origin.json sidecar (original_filename); without
                           it File extension (.xlsx) (77) raises: the staged name is not the
                           delivered one. A malformed sidecar (read_origin: not JSON, not an
                           object, no non-empty original_filename) raises the same way, named
                           as malformed; when 77 is not graded it is only recorded.
  requires_external_links  always False (maintainer: no task requires external links).

Artefacts
  task_folder/det_checks.json  everything (config, gate, task_meta, the full verdicts and the
                               full recalculation block, or the failure and its stage);
                               written in every case, also when a config error, a rubric
                               drift or a suitability refusal stops the run;
                               judge._finalize_case copies it into the output dir
  scored_results.det_checks    compact block (status, mode, per-check engine / decision / live /
                               n_mistakes / summary capped at DB_SUMMARY_CAP characters) and the
                               recalculation block `values` ONCE (no local value_path)
  stats in harness_verdicts    -> scored_results.accuracy_engine.checks[key].stats: a value check's
                               stats.values is the reference {"ref": "det_checks.values"}; in the
                               DB payload every list longer than DB_LIST_CAP keeps its first
                               DB_LIST_CAP items, the original lengths under "db_capped"
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path

from detchecks.errors import GradingError

try:
    from .logger import logger
    from .misc_utils import current_benchmark, load_env_var
    from . import rubric_suitability, workbook_properties
except ImportError:  # imported as a bare module (utils/ on sys.path)
    from logger import logger
    from misc_utils import current_benchmark, load_env_var
    import rubric_suitability
    import workbook_properties

FAMILY = "det_checks"
MODES = ("harness", "llm", "off")
ATTEMPT_FILENAME = "ai_attempt.xlsx"
ARTEFACT_FILENAME = "det_checks.json"
RECALC_DIRNAME = "det_checks_recalc"

# The name each rubric number must carry (rubric_9 flat numbering, as in
# rubric_suitability.RETIRED_CHECK_NAMES). The config lists decide what runs and what counts;
# a pin only permits it. A number missing here, or whose rubric position or detchecks key
# carries another name, refuses to grade.
DET_CHECK_NAMES = {
    22: ("Error Checks", "No formula errors"),
    29: ("Error Checks", "Clean Name Manager"),
    47: ("Formatting", "No bright-yellow highlighting"),
    49: ("Formatting", "Black font for calculations"),
    50: ("Formatting", "Green font for cross-sheet links"),
    51: ("Formatting", "Red font for external links"),
    61: ("Formatting", "Consistent zoom level"),
    62: ("Formatting", "Active cell reset to A1 on all sheets"),
    65: ("Formatting", "Negatives in parentheses"),
    66: ("Formatting", "Zeros as dashes"),
    69: ("Formatting", "Sufficient column widths"),
    70: ("Formatting", "Reasonable column widths"),
    73: ("Formatting", "Reasonable row heights"),
    74: ("Formatting", "No merged cells"),
    77: ("Formatting", "File extension (.xlsx)"),
    80: ("Formulas", "Avoid volatile functions"),
    87: ("Formulas", "Avoid whole-column references"),
    92: ("Potential Dangers", "No hidden sheets"),
    93: ("Potential Dangers", "No hidden rows/columns"),
    94: ("Potential Dangers", "No white-on-white hiding"),
    95: ("Potential Dangers", "No external links"),
}
_NUMBER_BY_KEY = {f"{cat}/{name}": no for no, (cat, name) in DET_CHECK_NAMES.items()}
_FILE_EXTENSION_CHECK = 77


class DetChecksError(GradingError):
    """The deterministic checks could not grade the delivered workbook. The grading must
    fail here, before the LLM (no fallback). Carries GradingError's check/path/failures/
    verdicts; the message names every failing check by title and the file."""


class DetChecksConfigError(Exception):
    """project_configs.yaml det_checks does not match the rubric or the detchecks registry."""


@dataclasses.dataclass(frozen=True)
class DetChecksSettings:
    enabled: bool
    live: tuple
    recorded_only: tuple
    excel_recalc: bool
    libreoffice_path: str
    libreoffice_timeout_s: float
    libreoffice_max_mb: float


@dataclasses.dataclass
class DetChecksRun:
    """What run_det_checks returns: the entries to merge into harness_verdicts, the compact
    block for scored_results.det_checks, and the artefact written into the task folder."""
    mode: str
    harness_verdicts: dict
    summary: dict
    artefact_path: Path | None = None

    def for_judge(self) -> dict:
        """The `det_checks` argument of judge.single_pass_judge_case / _finalize_case."""
        return {
            "mode": self.mode,
            "summary": self.summary,
            "artefact": str(self.artefact_path) if self.artefact_path else None,
        }


# --------------------------------------------------------------------------- config
def label(number: int) -> str:
    """The maintainer's naming convention: 'No hidden sheets (92)'."""
    pin = DET_CHECK_NAMES.get(int(number))
    return f"{pin[1]} ({number})" if pin else f"check {number}"


def label_for_key(key: str) -> str:
    no = _NUMBER_BY_KEY.get(key)
    return label(no) if no is not None else key


def _as_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _numbers(env_key: str) -> tuple:
    raw = str(load_env_var(env_key, default="") or "").strip()
    if not raw or raw.lower() in ("none", "null", "[]"):
        return ()
    out = []
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit():
            raise DetChecksConfigError(f"{env_key}: {tok!r} is not a check number")
        out.append(int(tok))
    if len(set(out)) != len(out):
        raise DetChecksConfigError(f"{env_key}: a check number is listed twice ({raw!r})")
    return tuple(out)


def load_settings() -> DetChecksSettings:
    """det_checks.* from project_configs.yaml (env BIZBENCHJUDGE_DET_CHECKS_*); the
    LibreOffice binary is the judge's own paths.libreoffice_path."""
    settings = _read_settings()
    if not settings.libreoffice_max_mb > 0 or not settings.libreoffice_timeout_s > 0:
        raise DetChecksConfigError(
            f"det_checks.libreoffice_max_mb ({settings.libreoffice_max_mb}) and "
            f"det_checks.libreoffice_timeout_seconds ({settings.libreoffice_timeout_s}) must be positive")
    return settings


def _read_settings() -> DetChecksSettings:
    return DetChecksSettings(
        enabled=_as_bool(load_env_var("DET_CHECKS_ENABLED", default="false")),
        live=_numbers("DET_CHECKS_LIVE"),
        recorded_only=_numbers("DET_CHECKS_RECORDED_ONLY"),
        excel_recalc=_as_bool(load_env_var("DET_CHECKS_EXCEL_RECALC", default="false")),
        libreoffice_path=str(load_env_var(
            "PATHS_LIBREOFFICE_PATH",
            default="/Applications/LibreOffice.app/Contents/MacOS/soffice")),
        libreoffice_timeout_s=float(load_env_var("DET_CHECKS_LIBREOFFICE_TIMEOUT_SECONDS", default=600)),
        libreoffice_max_mb=float(load_env_var("DET_CHECKS_LIBREOFFICE_MAX_MB", default=10)),
    )


def resolve_mode(cli_value: str | None = None, settings: DetChecksSettings | None = None) -> str:
    """--det-checks value, else the config default (enabled -> harness, disabled -> off)."""
    if cli_value is not None:
        if cli_value not in MODES:
            raise ValueError(f"--det-checks must be one of {MODES}, got {cli_value!r}")
        return cli_value
    settings = settings or load_settings()
    return "harness" if settings.enabled else "off"


def add_det_checks_arg(parser) -> None:
    """--det-checks harness|llm|off (judge v13). Shared by every single-pass driver."""
    parser.add_argument(
        "--det-checks",
        dest="det_checks",
        choices=list(MODES),
        default=None,
        help=(
            "(Single-pass) Deterministic rubric checks (utils/det_checks.py): 'harness' = the "
            "Python verdicts of the live checks replace the LLM's in the recorded total (judge "
            "v13); 'llm' = they run and are recorded beside the LLM's, which count (shadow); "
            "'off' = they do not run. Default: project_configs.yaml det_checks.enabled "
            "(true -> harness). A check that cannot grade the file fails the attempt before "
            "the LLM call in both 'harness' and 'llm' (no fallback)."
        ),
    )


def configured_checks(settings: DetChecksSettings, rubric: dict, rubric_src=None) -> dict:
    """{number: live?} for the configured checks, validated against the pins, the loaded
    rubric (read from `rubric_src`, named in the errors) and the detchecks registry. Positions
    beyond the rubric (v1's 17-check rubric_8) are dropped: those checks do not exist there.
    Raises DetChecksConfigError."""
    from detchecks.checks import REGISTRY

    overlap = sorted(set(settings.live) & set(settings.recorded_only))
    if overlap:
        raise DetChecksConfigError(
            f"det_checks: {[label(n) for n in overlap]} listed as both live and recorded_only")
    flat = [(cat, c["name"]) for cat, checks in rubric.items() for c in checks]
    where = f"the loaded rubric ({rubric_src})" if rubric_src else "the loaded rubric"
    plan = {}
    for no in list(settings.live) + list(settings.recorded_only):
        live = no in settings.live
        pin = DET_CHECK_NAMES.get(no)
        if pin is None:
            named = f"{flat[no - 1][1]} ({no})" if 0 < no <= len(flat) else f"check {no} (beyond {where})"
            raise DetChecksConfigError(
                f"det_checks names {named}, which has no entry in DET_CHECK_NAMES "
                f"(utils/det_checks.py) - add the (category, name) pin first")
        cls = REGISTRY.get(no)
        if cls is None:
            raise DetChecksConfigError(f"det_checks names {label(no)}, which detchecks does not implement")
        if cls.key != f"{pin[0]}/{pin[1]}":
            raise DetChecksConfigError(
                f"{label(no)} is pinned to {pin!r} but detchecks registers {cls.key!r} under that number "
                f"- refusing to grade")
        if no > len(flat):
            continue
        if flat[no - 1] != pin:
            raise DetChecksConfigError(
                f"{label(no)} is pinned to {pin!r} but {where} has {flat[no - 1]!r} at that position "
                f"- numbering drift; refusing to grade")
        if live and not getattr(cls, "live", True):
            raise DetChecksConfigError(
                f"det_checks.live lists {label(no)}, but detchecks marks it not live (Check.live = False); "
                f"move it to det_checks.recorded_only or change the check")
        plan[no] = live
    return plan


def startup_check(rubric_path, mode: str | None = None) -> str:
    """Validate det_checks against the run's rubric once, before a driver grades anything,
    and log what will run. Returns the resolved mode; raises DetChecksConfigError, so a bad
    config stops the run before any download or API call (rather than failing every
    attempt one by one)."""
    settings = load_settings()
    mode = resolve_mode(mode, settings)
    if mode == "off":
        logger.info("det_checks: off - the LLM decides every check (judge v13 rows without Python verdicts)")
        return mode
    _install_termination_reaper()
    rubric = json.loads(Path(rubric_path).read_text(encoding="utf-8"))
    plan = configured_checks(settings, rubric, rubric_path)
    logger.info(
        f"det_checks: mode={mode}; live: {[label(n) for n, live in sorted(plan.items()) if live]}; "
        f"recorded only: {[label(n) for n, live in sorted(plan.items()) if not live]}; "
        f"LibreOffice {settings.libreoffice_path} (files up to {settings.libreoffice_max_mb:g} MB, "
        f"{settings.libreoffice_timeout_s:.0f} s); Excel recalculation "
        f"{'ON' if settings.excel_recalc else 'off'}")
    return mode


# --------------------------------------------------------------------------- gate
def _judge_rubric(task_folder: Path, rubric_path) -> tuple[dict, Path]:
    """The rubric the judge itself will load: a task folder's own rubric.json wins over the
    configured one (excel_utils.copy_support_files -> output_dir/rubric.json)."""
    local = Path(task_folder) / "rubric.json"
    src = local if local.exists() else Path(rubric_path)
    return json.loads(src.read_text(encoding="utf-8")), src


def scored_checks(task_folder, rubric: dict, weights: dict, benchmark) -> tuple[set, dict]:
    """{(category, name)} the judge scores for this task, and the suitability provenance:
    the applicable checks after rubric_suitability.load_for_case (annotation + retired
    checks; the same call single_pass_judge_case makes) that the effective weights still
    score. Raises SuitabilityError exactly where the judge would."""
    suit = rubric_suitability.load_for_case(Path(task_folder), rubric, benchmark)
    if suit is None:
        applicable = {(cat, c["name"]) for cat, checks in rubric.items() for c in checks}
        effective = weights
        provenance = {"gated": False}
        if rubric_suitability.skip_requested():
            provenance["skipped_via_env"] = True
    else:
        applicable = {(cat, n) for cat, names in suit["applicable"].items() for n in names}
        effective = rubric_suitability.build_effective_weights(weights, suit["excluded"])
        p = suit.get("provenance") or {}
        provenance = {k: p.get(k) for k in ("gated", "s3_key", "annotator", "excluded_count",
                                             "retired_checks", "skipped_via_env") if k in p}
    weighted = {
        (cat, e["name"])
        for cat, entries in (effective or {}).items()
        if cat != "CategoryWeights" and isinstance(entries, list)
        for e in entries
        if isinstance(e, dict) and "name" in e
    }
    return applicable & weighted, provenance


# --------------------------------------------------------------------------- helpers
def merge_harness_verdicts(base: dict | None, det: dict | None) -> dict:
    """The answer check's entries plus the deterministic family's. Keys are disjoint by
    construction (Accuracy/* and Rounding/* vs the detchecks keys); an overlap is a bug."""
    merged = dict(base or {})
    overlap = sorted(set(merged) & set(det or {}))
    if overlap:
        raise ValueError(f"harness_verdicts: answer-check and deterministic-check keys overlap: {overlap}")
    merged.update(det or {})
    return merged


def json_safe(obj):
    """A copy of `obj` made of JSON-native values only: scores.json is dumped without
    default= and scored_results goes into a JSONB column (no NaN, no tuples or sets as keys,
    no str subclasses such as detchecks' ExcelError)."""
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj) if math.isfinite(obj) else str(obj)
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [json_safe(v) for v in obj]
        try:
            return sorted(items)
        except TypeError:
            return sorted(items, key=repr)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_safe(dataclasses.asdict(obj))
    if isinstance(obj, os.PathLike):
        return os.fspath(obj)
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", "replace")
    return str(obj)


_CODE_SHA = None
# The grading code: what code_sha fingerprints. Nothing else under detchecks/ (scratch/ is
# git-ignored and full of probes, tests/, tools/, docs/, out/) may move the fingerprint, so the
# same commit always records the same code_sha.
CODE_SHA_FILES = ("__init__.py", "api.py", "errors.py")
CODE_SHA_DIRS = ("core", "checks")


def code_sha_files(root) -> list:
    """The .py files code_sha hashes under a detchecks package folder `root`, in hashing order."""
    root = Path(root)
    files = [root / f for f in CODE_SHA_FILES if (root / f).is_file()]
    for d in CODE_SHA_DIRS:
        files += [p for p in (root / d).rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def code_sha_of(root) -> str:
    """Fingerprint of the grading code in the detchecks package folder `root`: relative path and
    bytes of detchecks/__init__.py, api.py, errors.py and every .py under core/ and checks/."""
    root = Path(root)
    h = hashlib.sha256()
    for p in code_sha_files(root):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def code_sha() -> str:
    """Fingerprint of the grading code (code_sha_of the imported detchecks), recorded per grading."""
    global _CODE_SHA
    if _CODE_SHA is None:
        import detchecks

        _CODE_SHA = code_sha_of(Path(detchecks.__file__).resolve().parent)
    return _CODE_SHA


# --------------------------------------------------------------------------- DB payload
# What lands in gradings.scored_results (and scores.json): the recalculation block `values` once,
# at scored_results.det_checks.values, each value check's stats.values a reference to it (it was
# copied into every value check: 7 identical copies, ~9 KB each with a LibreOffice gap list), and
# every list longer than DB_LIST_CAP cut to its first DB_LIST_CAP items, the original length
# recorded under "db_capped". det_checks.json (the bundle) keeps the full verdicts and the full
# values block.
DB_LIST_CAP = 10
DB_SUMMARY_CAP = 300
VALUES_REF = "det_checks.values"


def _cap_lists(obj, path: str, capped: dict):
    """A copy of `obj` (JSON-safe) whose lists keep their first DB_LIST_CAP items; `capped` gets
    {json path: original length} for every list cut."""
    if isinstance(obj, dict):
        return {k: _cap_lists(v, f"{path}.{k}" if path else k, capped) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > DB_LIST_CAP:
            capped[path] = len(obj)
            obj = obj[:DB_LIST_CAP]
        return [_cap_lists(v, f"{path}[{i}]", capped) for i, v in enumerate(obj)]
    return obj


def _db_capped(obj: dict) -> dict:
    capped: dict = {}
    out = _cap_lists(obj, "", capped)
    if capped:
        out["db_capped"] = capped
    return out


def _db_values(values: dict | None) -> dict | None:
    """The shared recalculation block as stored once in scored_results.det_checks.values: no local
    value_path (deleted with det_checks_recalc/), lists capped (gaps: first DB_LIST_CAP of n_gaps)."""
    if values is None:
        return None
    return _db_capped({k: v for k, v in values.items() if k != "value_path"})


def _db_stats(stats: dict, shared_values: dict | None) -> dict:
    """A check's stats as stored in scored_results.accuracy_engine.checks: stats.values -> the
    reference VALUES_REF when it is the shared block, long lists capped."""
    out = dict(stats)
    if shared_values is not None and out.get("values") == shared_values:
        out["values"] = {"ref": VALUES_REF}
    return _db_capped(out)


def _short(text, n: int = DB_SUMMARY_CAP) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 3] + "..."


def _file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _write_artefact(path: Path, record: dict) -> None:
    try:
        path.write_text(json.dumps(json_safe(record), indent=1, allow_nan=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"  [det_checks] could not write {path}: {e}")


def _size_guarded_libreoffice(limit_mb: float):
    """The recalculation pipeline's LibreOffice step (RecalcPolicy.lo_runner) behind the size
    limit (det_checks.libreoffice_max_mb; maintainer 2026-10-04: no LibreOffice run on a file over
    10 MB, memory). Above it the step raises GradingError, so the value checks fail and the
    grading stops loudly before the LLM call, like the formula-cache refusal. It only fires
    when LibreOffice is actually needed: an Excel-saved file of any size is read from its own
    caches and never gets here."""

    def _run(src, out_dir, policy):
        size = os.path.getsize(src)
        if size > limit_mb * 1_000_000:
            raise GradingError(
                f"{src} is {size / 1_000_000:.1f} MB, over det_checks.libreoffice_max_mb "
                f"({limit_mb:g} MB): a file not saved by Excel needs a LibreOffice recalculation "
                f"for its formula values, which is not run on files this large (memory) - "
                f"not graded: too large")
        from detchecks.core import recalc as det_recalc   # looked up per call (tests swap it)

        return det_recalc.libreoffice_recalc(src, out_dir, policy)

    return _run


def _reap_libreoffice(workdir: Path) -> list[int]:
    """Kill any LibreOffice process still running on one of this grading's private profiles.

    detchecks' libreoffice_recalc already kills its run on a timeout, an exception or an
    interrupt, runs soffice under a watchdog that kills it when the grader dies, and installs a
    SIGTERM handler (detchecks/core/recalc.py); this sweep is the last net when the checks return
    or raise. It matches the profile folder workdir/_lo_profiles/ in a command line as a raw
    path or as the percent-encoded file URL soffice is given (a task folder name with a space or
    '%'), and kills each match with everything below it. The profiles live under this attempt's
    own workdir, so the match can only hit this grading's processes - never another job's."""
    from detchecks.core import recalc as det_recalc

    marker = Path(workdir) / "_lo_profiles"
    killed = det_recalc.reap_profiles(str(marker))
    if killed:
        logger.warning(f"  [det_checks] killed leftover LibreOffice process(es) {killed} on {marker}{os.sep}")
    return killed


def _remove_recalc_dir(workdir: Path) -> None:
    """Delete this grading's det_checks_recalc/ (LibreOffice copies, profiles) once the checks
    have returned or raised: every driver gets it (grade_with_orchestration never called
    grade_from_db.prune_workbook_copies). det_checks.json lives in the task folder and stays."""
    workdir = Path(workdir)
    if workdir.name != RECALC_DIRNAME or not workdir.exists():
        return
    shutil.rmtree(workdir, ignore_errors=True)
    if workdir.exists():
        logger.warning(f"  [det_checks] could not remove {workdir}")


def _install_termination_reaper() -> None:
    """detchecks' SIGTERM / SIGHUP handler (kill this process's LibreOffice runs, then die of the
    signal as before). Main thread only (Python's rule): startup_check runs there in every DB
    driver before any worker starts, and judge.py grades in the main thread."""
    from detchecks.core import recalc as det_recalc

    det_recalc.install_termination_reaper()


def _delivered_text(delivered) -> str:
    return f"delivered as {delivered!r}" if delivered else "delivered file name unknown"


def read_origin(task_folder) -> tuple:
    """(sidecar, problem) for task_folder/_attempt_origin.json: problem None (it names the
    delivered file), "missing" (no sidecar) or "malformed: <why>" (unreadable or not JSON, a JSON
    value that is not an object, or no non-empty string 'original_filename'). The sidecar dict
    is returned whenever it is an object."""
    p = Path(task_folder) / workbook_properties.ORIGIN_FILENAME
    if not p.exists():
        return None, "missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return None, f"malformed: not readable as JSON ({type(e).__name__}: {str(e)[:120]})"
    if not isinstance(data, dict):
        return None, f"malformed: a JSON {type(data).__name__}, not an object"
    name = data.get("original_filename")
    if "original_filename" not in data:
        return data, "malformed: no 'original_filename'"
    if not isinstance(name, str) or not name.strip():
        return data, f"malformed: 'original_filename' is {name!r}, not a file name"
    return data, None


def _origin_note(task_folder: Path, problem: str | None) -> str:
    sidecar = Path(task_folder) / workbook_properties.ORIGIN_FILENAME
    if problem == "missing":
        why = f"no {workbook_properties.ORIGIN_FILENAME} in {task_folder}"
    else:
        why = f"{sidecar} is {problem}"
    return (f"({why}: the delivered file name is unknown, so {label(_FILE_EXTENSION_CHECK)} cannot be "
            f"graded)")


def _failure_message(e: GradingError, attempt: Path, delivered, task_folder: Path,
                     selected: list, settings: DetChecksSettings, origin_problem: str | None = None) -> str:
    failures = dict(getattr(e, "failures", None) or {})
    head = (f"deterministic checks could not grade {attempt} ({_delivered_text(delivered)}): "
            f"{len(failures) or 'one or more'} of {len(selected)} check(s) failed; no fallback "
            f"(judge v13), the grading stops before the LLM call")
    lines = [head]
    for key, msg in failures.items():
        lines.append(f"  - {label_for_key(key)}: {msg}")
    if not failures:
        lines.append(f"  - {e}")
    key77 = "/".join(DET_CHECK_NAMES[_FILE_EXTENSION_CHECK])
    if delivered is None and key77 in failures:
        lines.append("  " + _origin_note(task_folder, origin_problem or "missing"))
    if any("LibreOffice" in str(m) for m in (list(failures.values()) or [e])):
        lines.append(
            f"  (LibreOffice binary: {settings.libreoffice_path}, from project_configs.yaml "
            f"paths.libreoffice_path; timeout {settings.libreoffice_timeout_s:.0f} s, "
            f"det_checks.libreoffice_timeout_seconds; size limit {settings.libreoffice_max_mb:g} MB, "
            f"det_checks.libreoffice_max_mb)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- run
_UNSET = object()


def run_det_checks(task_folder, *, rubric_path, weights_path, mode: str | None = None,
                   benchmark=_UNSET) -> DetChecksRun:
    """Grade the applicable configured checks on task_folder/ai_attempt.xlsx (module doc).

    Returns a DetChecksRun; raises DetChecksError (no fallback) when a check cannot grade the
    file, DetChecksConfigError on a config/rubric/registry mismatch, and SuitabilityError where
    the judge itself would. Writes task_folder/det_checks.json in every case: the verdicts, the
    "off" / "no_applicable_checks" record, or status "error" with the stage that failed
    (config, gate, grade) and the error - whatever the exception, which is then re-raised as is."""
    t0 = time.perf_counter()
    task_folder = Path(task_folder)
    artefact = task_folder / ARTEFACT_FILENAME
    ctx = {"stage": "config", "written": False,
           "record": {"status": None, "mode": mode, "file": {"path": str(task_folder / ATTEMPT_FILENAME)}}}
    try:
        return _run_det_checks(task_folder, artefact, rubric_path, weights_path, mode, benchmark, t0, ctx)
    except BaseException as e:
        if not ctx["written"]:              # the grade stage writes its own, fuller record
            rec = dict(ctx["record"])
            rec.update(status="error", stage=ctx["stage"], error=f"{type(e).__name__}: {e}",
                       seconds=round(time.perf_counter() - t0, 3))
            _write_artefact(artefact, rec)
        raise


def _run_det_checks(task_folder: Path, artefact: Path, rubric_path, weights_path, mode, benchmark, t0,
                    ctx: dict) -> DetChecksRun:
    settings = load_settings()
    mode = resolve_mode(mode, settings)
    ctx["record"]["mode"] = mode
    attempt = task_folder / ATTEMPT_FILENAME
    if mode == "off":
        summary = {"status": "off", "mode": "off"}
        _write_artefact(artefact, {**summary, "note": "det_checks off: the LLM decides every check"})
        ctx["written"] = True
        logger.info("  [det_checks] off: nothing graded, the LLM decides every check")
        return DetChecksRun("off", {}, summary, artefact)

    from detchecks import api as det_api
    from detchecks.core.recalc import RecalcPolicy

    rubric, rubric_src = _judge_rubric(task_folder, rubric_path)
    ctx["record"]["rubric"] = str(rubric_src)
    plan = configured_checks(settings, rubric, rubric_src)
    ctx["stage"] = "gate"
    weights = json.loads(Path(weights_path).read_text(encoding="utf-8"))
    if benchmark is _UNSET:
        benchmark = current_benchmark(required=False)
    scored, suitability = scored_checks(task_folder, rubric, weights, benchmark)
    selected = [no for no in sorted(plan) if DET_CHECK_NAMES[no] in scored]
    not_applicable = [no for no in sorted(plan) if no not in selected]

    origin, origin_problem = read_origin(task_folder)
    delivered = None if origin_problem else origin["original_filename"]
    task_meta = {"requires_external_links": False}
    if delivered:
        task_meta["delivered_filename"] = delivered
    if origin_problem and _FILE_EXTENSION_CHECK in selected:
        logger.warning(f"  [det_checks] {_origin_note(task_folder, origin_problem)}")
    elif origin_problem and origin_problem != "missing":
        logger.warning(f"  [det_checks] {task_folder / workbook_properties.ORIGIN_FILENAME} is {origin_problem}; "
                       f"{label(_FILE_EXTENSION_CHECK)} is not graded for this task, so nothing depends on it")
    workdir = task_folder / RECALC_DIRNAME
    policy = RecalcPolicy(
        workdir=str(workdir),
        libreoffice_path=settings.libreoffice_path,
        lo_timeout_s=settings.libreoffice_timeout_s,
        excel_allowed=settings.excel_recalc,
        lo_runner=_size_guarded_libreoffice(settings.libreoffice_max_mb),
    )
    record = {
        "status": None,
        "mode": mode,
        "code_sha": code_sha(),
        "file": {
            "path": str(attempt),
            "bytes": attempt.stat().st_size if attempt.exists() else None,
            "sha256": _file_sha256(attempt),
            "delivered_filename": delivered,
            "origin_sidecar": origin_problem is None,
            "origin_problem": origin_problem,
        },
        "config": {
            "live": list(settings.live),
            "recorded_only": list(settings.recorded_only),
            "excel_recalc": settings.excel_recalc,
            "libreoffice_path": settings.libreoffice_path,
            "libreoffice_timeout_s": settings.libreoffice_timeout_s,
            "libreoffice_max_mb": settings.libreoffice_max_mb,
            "workdir": str(workdir),
        },
        "rubric": str(rubric_src),
        "suitability": suitability,
        "task_meta": task_meta,
        "graded": selected,
        "not_applicable": not_applicable,
    }
    ctx["record"] = record
    ctx["stage"] = "grade"
    base_summary = {"mode": mode, "code_sha": record["code_sha"], "delivered_filename": delivered,
                    "graded": selected, "not_applicable": not_applicable, "artefact": ARTEFACT_FILENAME}

    if not selected:
        record["status"] = "no_applicable_checks"
        record["seconds"] = round(time.perf_counter() - t0, 3)
        _write_artefact(artefact, record)
        ctx["written"] = True
        logger.info(f"  [det_checks] mode={mode}: no configured check is applicable for this task "
                    f"({len(not_applicable)} not applicable)")
        summary = {"status": "no_applicable_checks", **base_summary, "checks": {},
                   "seconds": record["seconds"]}
        return DetChecksRun(mode, {}, json_safe(summary), artefact)

    logger.info(f"  [det_checks] mode={mode}: grading {len(selected)} check(s) on {attempt.name} "
                f"({_delivered_text(delivered)}); not applicable: {[label(n) for n in not_applicable] or 'none'}")
    _install_termination_reaper()
    try:
        verdicts = det_api.grade(str(attempt), checks=selected, task_meta=task_meta, recalc=policy)
        missing = sorted(set("/".join(DET_CHECK_NAMES[n]) for n in selected) - set(verdicts))
        if missing:
            raise GradingError(f"detchecks returned no verdict for {missing} on {attempt}",
                               path=str(attempt), failures={k: "no verdict returned" for k in missing})
    except GradingError as e:
        record["status"] = "error"
        record["stage"] = "grade"
        record["error"] = str(e)
        record["failures"] = dict(e.failures or {})
        record["finished_verdicts"] = dict(e.verdicts or {})
        record["seconds"] = round(time.perf_counter() - t0, 3)
        _write_artefact(artefact, record)
        ctx["written"] = True
        msg = _failure_message(e, attempt, delivered, task_folder, selected, settings, origin_problem)
        logger.error(f"  [det_checks] {msg}")
        raise DetChecksError(msg, check=e.check, path=str(attempt), failures=e.failures,
                             verdicts=e.verdicts) from e
    except Exception as e:  # noqa: BLE001 - anything else is just as loud, with the artefact written
        record["status"] = "error"
        record["stage"] = "grade"
        record["error"] = f"{type(e).__name__}: {e}"
        record["seconds"] = round(time.perf_counter() - t0, 3)
        _write_artefact(artefact, record)
        ctx["written"] = True
        msg = (f"deterministic checks could not grade {attempt} ({_delivered_text(delivered)}): "
               f"{type(e).__name__}: {e}; no fallback (judge v13), the grading stops before the LLM call")
        logger.error(f"  [det_checks] {msg}")
        raise DetChecksError(msg, path=str(attempt)) from e
    finally:
        _reap_libreoffice(workdir)
        _remove_recalc_dir(workdir)

    all_stats = {no: json_safe(verdicts["/".join(DET_CHECK_NAMES[no])].get("stats") or {}) for no in selected}
    # the recalculation block: one per grading (detchecks attaches the same one to every value check)
    values = next((s["values"] for s in all_stats.values() if isinstance(s.get("values"), dict)), None)
    harness_verdicts, checks_block = {}, {}
    for no in selected:
        key = "/".join(DET_CHECK_NAMES[no])
        v = verdicts[key]
        live = plan[no] and bool(v.get("live", True))
        stats = all_stats[no]
        mistakes = json_safe(list(v.get("mistakes") or []))
        entry = {
            "engine": "harness" if live else "llm",
            "decision": v["decision"],
            "summary": str(v.get("summary") or ""),
            "mistakes": mistakes,
            "fallback_reason": None if live else (v.get("live_note") or det_api.LIVE_NOTE),
            "family": FAMILY,
            "live": live,
            "check_no": no,
            "n_mistakes": stats.get("n_mistakes", len(mistakes)),
            "stats": _db_stats(stats, values),
        }
        harness_verdicts[key] = entry
        checks_block[key] = {"check_no": no, "engine": entry["engine"], "decision": entry["decision"],
                             "live": live, "n_mistakes": entry["n_mistakes"], "summary": _short(entry["summary"])}

    seconds = round(time.perf_counter() - t0, 3)
    record.update(status="ok", seconds=seconds, values=values, verdicts=verdicts)
    _write_artefact(artefact, record)
    ctx["written"] = True
    summary = json_safe({"status": "ok", **base_summary, "values": _db_values(values), "checks": checks_block,
                         "db_list_cap": DB_LIST_CAP, "seconds": seconds})
    fails = [label(e["check_no"]) + ("" if e["live"] else " [recorded only]")
             for e in harness_verdicts.values() if e["decision"] == "fail"]
    logger.info(f"  [det_checks] {len(selected)} graded in {seconds:.1f} s "
                f"(values: {(values or {}).get('source') or 'not needed'}); "
                f"fail: {fails or 'none'}")
    return DetChecksRun(mode, harness_verdicts, summary, artefact)
