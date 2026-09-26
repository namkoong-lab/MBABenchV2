"""Offline checks for the local source and sink (utils/local_store.py).

Run from judge/:  python tests_offline/test_offline_source_sink.py
No DB, S3 or LLM access. The four things that have to hold for an offline
grading to mean the same as a cloud one:

  1. a bundled row's repo-relative paths resolve to the files the manifest
     hashes — the judge must read the golden the bundle shipped, not a
     lookalike;
  2. staging a local attempt produces the same task folder staging an S3
     attempt does, so nothing below setup_task_folder can tell them apart;
  3. a local grading row is the `gradings` row, column for column;
  4. the local path needs neither a database driver nor credentials.

Test 2 proves its claim rather than asserting a remembered layout: the same
attempt is staged twice, once through local paths and once through s3://
sources with the download monkeypatched to copy from the same fixture, and
the two folder listings must be identical. The listing is also pinned to a
fixture so a future change to the layout has to be deliberate.
"""

import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1]
REPO = JUDGE.parent
sys.path.insert(0, str(JUDGE))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Two checks deliberately point the config lookup at an empty directory.
logging.getLogger("utils.repo_config").setLevel(logging.ERROR)

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# A synthetic bundle, so these checks run before (and independently of) the
# real one. Same shapes as data/README.md describes.
# ---------------------------------------------------------------------------

TASK_ID = 1
ATTEMPT_ID = 9_000_000_001


def build_bundle(root: Path) -> dict:
    """A one-task, one-attempt bundle under `root`; returns its manifest."""
    from openpyxl import Workbook

    def book(path: Path, title: str, cells: dict):
        wb = Workbook()
        ws = wb.active
        ws.title = title
        for ref, value in cells.items():
            ws[ref] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)

    data = root / "data"
    task = data / "tasks" / f"task_id={TASK_ID}"
    book(task / "starting_files" / "Case.xlsx", "Inputs", {"A1": "Revenue", "B1": 100})
    book(
        task / "solution_files" / "Case_solution.xlsx",
        "Model",
        {"A1": "Revenue", "B1": 100, "A2": "Cost", "B2": 40, "A3": "Profit", "B3": 60},
    )
    (task / "starting_files" / "Questions.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (task / "task.json").write_text(
        json.dumps(
            {
                "id": TASK_ID,
                "task_name": "Synthetic Case",
                "task_starting_files": [
                    {
                        "name": "Case.xlsx",
                        "path": f"data/tasks/task_id={TASK_ID}/starting_files/Case.xlsx",
                    },
                    {
                        "name": "Questions.pdf",
                        "path": f"data/tasks/task_id={TASK_ID}/starting_files/Questions.pdf",
                    },
                ],
                "task_solution_files": [
                    {
                        "name": "Case_solution.xlsx",
                        "path": f"data/tasks/task_id={TASK_ID}/solution_files/Case_solution.xlsx",
                    }
                ],
                "deprecated": False,
            }
        )
    )

    attempt_dir = data / "results" / "attempts" / str(ATTEMPT_ID)
    book(attempt_dir / "solution.xlsx", "Model", {"A1": "Revenue", "B1": 100})
    (data / "results" / "task_attempts.jsonl").write_text(
        json.dumps(
            {
                "id": ATTEMPT_ID,
                "task_id": TASK_ID,
                "agent_model_name": "synthetic/agent",
                "agent_model_type": "api",
                "attempt_files": [
                    {
                        "name": "solution.xlsx",
                        "path": f"data/results/attempts/{ATTEMPT_ID}/solution.xlsx",
                    }
                ],
                "prompt_files": [],
                "prompt_version": 1609,
                "agent_failed": False,
                "deprecated": False,
            }
        )
        + "\n"
    )

    # Same shape as the real data/MANIFEST.json: a list of entries.
    entries = [
        {
            "path": p.relative_to(root).as_posix(),
            "s3_key": None,
            "size": p.stat().st_size,
            "sha256": sha256(p),
        }
        for p in sorted(data.rglob("*"))
        if p.is_file() and p.name != "MANIFEST.json"
    ]
    (data / "MANIFEST.json").write_text(
        json.dumps({"files": len(entries), "bytes": 0, "entries": entries}, indent=1)
    )
    return {e["path"]: e for e in entries}


# ---------------------------------------------------------------------------
# 1. Bundled paths resolve, and resolve to what the manifest hashed
# ---------------------------------------------------------------------------


def test_paths_resolve_to_manifest_hashes(local_store, root: Path, manifest: dict):
    task = local_store.load_task(TASK_ID)
    attempt = local_store.build_attempt(
        next(iter(local_store.attempt_index().values()))[0], task
    )

    seen = 0
    for column in ("task_solution_files", "task_starting_files", "attempt_files"):
        for ref in attempt[column] or []:
            path = Path(ref["path"])
            check(path.is_absolute(), f"{column}: {ref['name']} did not resolve absolute")
            check(path.is_file(), f"{column}: {ref['name']} missing at {path}")
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            entry = manifest.get(rel)
            check(entry is not None, f"{rel} is not in the manifest")
            if entry:
                check(
                    sha256(path) == entry["sha256"],
                    f"{rel}: sha256 does not match the manifest",
                )
                seen += 1
    check(seen >= 3, f"expected the golden, the starting file and the attempt; saw {seen}")

    # The same check against the real bundle, once it is in the checkout.
    real = REPO / "data" / "MANIFEST.json"
    if not real.is_file():
        print("  (skip) no data/MANIFEST.json in this checkout yet")
        return
    # The workbooks are not tracked; scripts/install_task_files.py unpacks them.
    if not (REPO / "data" / "tasks" / "task_id=1" / "starting_files" / "ApfelInc.xlsx").is_file():
        print("  (skip) task workbooks not installed — see README 'Task files' (scripts/install_task_files.py)")
        return
    by_path = {e["path"]: e for e in json.loads(real.read_text())["entries"]}
    bundled_task = json.loads((REPO / "data" / "tasks" / f"task_id={TASK_ID}"
                               / "task.json").read_text())
    checked = 0
    for column in ("task_solution_files", "task_starting_files"):
        for ref in bundled_task.get(column) or []:
            rel = ref["path"] if isinstance(ref, dict) else ref
            path = REPO / rel  # the real bundle, not the synthetic root
            check(path.is_file(), f"bundle: {rel} missing at {path}")
            entry = by_path.get(rel)
            check(entry is not None, f"bundle: {rel} is not in MANIFEST.json")
            if path.is_file() and entry:
                check(sha256(path) == entry["sha256"], f"bundle: {rel} sha256 mismatch")
                check(path.stat().st_size == entry["size"], f"bundle: {rel} size mismatch")
                checked += 1
    check(checked >= 2, f"expected task {TASK_ID}'s golden and starting file; saw {checked}")
    print(f"  bundle: {checked} task {TASK_ID} file(s) match MANIFEST.json")


# ---------------------------------------------------------------------------
# 2. A locally staged task folder is an S3-staged task folder
# ---------------------------------------------------------------------------


def listing(folder: Path) -> list:
    return sorted(
        p.relative_to(folder).as_posix() + ("/" if p.is_dir() else "")
        for p in folder.rglob("*")
    )


def test_task_folder_layout_matches_s3(local_store, gfd, root: Path, tmp: Path):
    task = local_store.load_task(TASK_ID)
    row = next(iter(local_store.attempt_index().values()))[0]
    local_attempt = local_store.build_attempt(row, task)

    local_dir = tmp / "staged_local"
    local_dir.mkdir()
    got_local = gfd.setup_task_folder(local_attempt, local_dir)
    check(got_local is not None, "local staging returned None")

    # The same attempt as the database would hand it over: s3:// sources, with
    # the download replaced by a copy out of the same bundle. Nothing below
    # setup_task_folder may be able to tell the two apart.
    def to_s3(refs):
        if not refs:
            return refs
        return [
            {
                "name": r["name"],
                "path": "s3://<bucket>/"
                + Path(r["path"]).relative_to(root).as_posix(),
            }
            for r in refs
        ]

    s3_attempt = dict(local_attempt)
    for column in ("attempt_files", "task_solution_files", "task_starting_files"):
        s3_attempt[column] = to_s3(local_attempt[column])

    real_download = gfd.download_file

    def fake_download(source, dest_path, base_dir=None):
        source = str(source)
        if source.startswith("s3://"):
            key = source.split("/", 3)[3]
            shutil.copy(str(root / key), str(dest_path))
            return
        return real_download(source, dest_path, base_dir=base_dir)

    gfd.download_file = fake_download
    try:
        s3_dir = tmp / "staged_s3"
        s3_dir.mkdir()
        got_s3 = gfd.setup_task_folder(s3_attempt, s3_dir)
    finally:
        gfd.download_file = real_download
    check(got_s3 is not None, "s3 staging returned None")
    if got_local is None or got_s3 is None:
        return

    check(
        got_local.name == got_s3.name,
        f"task folder name differs: {got_local.name} vs {got_s3.name}",
    )
    local_files = [p for p in listing(got_local) if not p.endswith(".log")]
    s3_files = [p for p in listing(got_s3) if not p.endswith(".log")]
    check(
        local_files == s3_files,
        f"staged layout differs:\n  local: {local_files}\n  s3:    {s3_files}",
    )

    # Same bytes, not just the same names: a wrong-but-present golden would
    # pass a name-only comparison and grade the attempt against the wrong file.
    for rel in local_files:
        a, b = got_local / rel, got_s3 / rel
        if a.is_file() and b.is_file() and not rel.endswith("_attempt_origin.json"):
            check(sha256(a) == sha256(b), f"{rel}: staged bytes differ")

    # Pin the layout so changing it has to be deliberate.
    fixture = FIXTURES / "task_folder_layout.json"
    expected = json.loads(fixture.read_text())["entries"]
    check(
        local_files == expected,
        f"staged layout drifted from {fixture.name}:\n"
        f"  got:      {local_files}\n  expected: {expected}",
    )


# ---------------------------------------------------------------------------
# 3. A local grading row is the gradings row
# ---------------------------------------------------------------------------


def test_grading_row_columns(local_store, gfd, tmp: Path):
    fixture = json.loads((FIXTURES / "db_columns.json").read_text())
    db_columns = fixture["columns"]["public.gradings"]
    check(
        list(local_store.GRADING_COLUMNS) == db_columns,
        f"GRADING_COLUMNS != the gradings table:\n"
        f"  code:     {list(local_store.GRADING_COLUMNS)}\n"
        f"  database: {db_columns}",
    )

    # And a row that actually goes through the sink carries exactly them.
    output_dir = tmp / "judge_output"
    (output_dir / "sub").mkdir(parents=True)
    (output_dir / "ai_judgement.json").write_text("{}")
    (output_dir / "sub" / "scores.json").write_text("{}")

    attempt = {"attempt_id": ATTEMPT_ID, "task_id": TASK_ID}
    result = {
        "success": True,
        "output_dir": str(output_dir),
        "scores": {"accuracy_grade": 1.0, "formula_grade": 2.0, "format_grade": 3.0},
        "scored_results": {"total_score": 42.0},
        "ai_judgement": {"Accuracy": []},
        "conversation": [{"role": "user", "content": "x"}],
        "elapsed_seconds": 90,
        "cost": 1.23456789,
        "versions": {"PROMPT_VERSION": "8", "JUDGE_VERSION": "12"},
        "raw_files_path": str(output_dir),
        "raw_files": [],
    }
    grading_id = gfd.write_grading_locally(
        attempt, result, "openai/gpt-5.6-sol", agentic=True, benchmark="v2"
    )

    path = local_store.gradings_dir("v2") / local_store.GRADINGS_FILENAME
    check(path.is_file(), f"{path} was not written")
    if not path.is_file():
        return
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    check(len(rows) == 1, f"expected 1 local grading row, got {len(rows)}")
    row = rows[0]
    check(list(row) == db_columns, f"written row columns {list(row)} != {db_columns}")
    check(row["id"] == grading_id, "row id is not the returned grading id")
    check(row["attempt_id"] == ATTEMPT_ID, "row attempt_id")
    check(row["judge_version"] == "12", "row judge_version")
    check(row["agentic_mode"] is True, "row agentic_mode")
    check(row["deprecated"] is False, "row deprecated")
    check(row["updated_at"] is None, "row updated_at should be null")
    check(
        isinstance(row["scored_results"], dict) and isinstance(row["raw_files"], list),
        "JSON columns must be nested, not serialised strings (data/README.md)",
    )
    check(
        row["created_at"].endswith("+00:00"),
        f"created_at {row['created_at']!r} needs an ISO-8601 offset",
    )

    # The staged files are the ones raw_files lists, under the grading's folder.
    check(
        row["raw_files_path"].endswith(f"gradings/{grading_id}"),
        f"raw_files_path {row['raw_files_path']!r} is not the grading folder",
    )
    staged = local_store.gradings_dir("v2") / str(grading_id)
    on_disk = sorted(
        p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()
    )
    check(row["raw_files"] == on_disk, f"raw_files {row['raw_files']} != {on_disk}")

    # Two gradings in the same millisecond must not share a folder.
    ids = {local_store.new_local_id() for _ in range(500)}
    check(len(ids) == 500, "local ids collided")


# ---------------------------------------------------------------------------
# 4. The local path needs no driver and no credentials
# ---------------------------------------------------------------------------


def test_import_without_cloud():
    """Re-import grade_from_db in a child process with boto3/psycopg2 poisoned."""
    import subprocess

    program = r"""
import builtins, importlib.util, sys
from pathlib import Path
JUDGE = Path(sys.argv[1])
sys.path.insert(0, str(JUDGE))
_real = builtins.__import__
def guard(name, *a, **k):
    if name.split(".")[0] in ("boto3", "botocore", "psycopg2"):
        raise AssertionError("offline import touched " + name)
    return _real(name, *a, **k)
builtins.__import__ = guard
spec = importlib.util.spec_from_file_location(
    "gfd_offline", JUDGE / "main_scripts" / "grade_from_db.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
builtins.__import__ = _real
# The offline entry points must be reachable without either library.
for name in ("resolve_io_mode", "write_grading_locally", "build_grading_row",
             "setup_task_folder", "expand_run_config"):
    assert hasattr(mod, name), name
print("OK")
"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "DATABASE_URL",
        )
    }
    # No monorepo config either: a reviewer's checkout has no database URL.
    env["SPREADSHEETSMITH_CONFIG_DIR"] = tempfile.mkdtemp()
    proc = subprocess.run(
        [sys.executable, "-c", program, str(JUDGE)],
        capture_output=True,
        text=True,
        env=env,
    )
    check(
        proc.returncode == 0 and "OK" in proc.stdout,
        f"importing grade_from_db offline failed:\n{proc.stdout}\n{proc.stderr}",
    )


def test_selection_rule(gfd):
    """--source/--sink win; otherwise a resolvable URL decides, and it is logged."""

    class Args:
        benchmark = "v2"
        source = None
        sink = None

    args = Args()
    args.source, args.sink = "local", "db"
    check(
        gfd.resolve_io_mode(args)[:2] == ("local", "db"),
        "explicit --source/--sink not honoured",
    )

    args.source = args.sink = None
    saved = dict(os.environ)
    try:
        os.environ["SPREADSHEETSMITH_CONFIG_DIR"] = tempfile.mkdtemp()  # no config
        os.environ.pop("DATABASE_URL", None)
        source, sink, why = gfd.resolve_io_mode(args)
        check(
            (source, sink) == ("local", "local"),
            f"no URL should default to local/local, got {source}/{sink}",
        )
        check("no database URL" in why, f"unhelpful log line: {why!r}")

        os.environ["DATABASE_URL"] = "postgresql://u:p@host/SpreadsheetSmith"
        source, sink, why = gfd.resolve_io_mode(args)
        check(
            (source, sink) == ("db", "db"),
            f"a resolvable URL must keep today's behaviour, got {source}/{sink}",
        )
        check("p@" not in why, "the log line leaks the password")

        # One end explicit, the other from the default.
        args.sink = "local"
        source, sink, _ = gfd.resolve_io_mode(args)
        check((source, sink) == ("db", "local"), f"mixed ends: {source}/{sink}")
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_run_config_overrides(gfd):
    """A typed attempt selection replaces the run config's, not collides with it.

    Both are members of argparse's exclusive group, so handing it the pair
    would abort with "not allowed with argument --attempt-ids-file" — which is
    the opposite of what --run-config promises about hand-typed flags.
    """
    config = Path(tempfile.mkdtemp(prefix="offline_run_config__")) / "cohort.yaml"
    config.write_text(
        "name: cohort\nargs:\n  benchmark: v2\n  single-pass: true\n  source: local\n  sink: local\n"
        "  attempt-ids-file: outputs/cohort_ids.json\n  model: openai/gpt-5.6-sol\n  accuracy-check: harness\n"
    )

    alone = gfd.expand_run_config(config, [])
    check("--attempt-ids-file" in alone, "the config should carry its own cohort")
    check("--single-pass" in alone, "the config's judge settings are missing")

    for typed in (["--attempt-ids", "1272"], ["--all-local"], ["--task-ids", "1"]):
        tokens = gfd.expand_run_config(config, typed)
        clash = [t for t in tokens if t in gfd.ATTEMPT_SELECTION_FLAGS]
        check(not clash, f"{typed[0]} should drop {clash} from the expansion")
        check(
            "--single-pass" in tokens and "--model" in tokens,
            f"{typed[0]} must keep the config's judge settings: {tokens}",
        )

    # And the pair actually parses, which is the behaviour that was broken.
    tokens = gfd.expand_run_config(config, ["--attempt-ids", "1272", "--dry-run"])
    check(
        "--attempt-ids" not in tokens and "--attempt-ids-file" not in tokens,
        f"expansion still names a cohort: {tokens}",
    )


def test_no_bucket_refusal(local_store):
    """No configured bucket is a refusal at the point of use, not a guess."""
    from utils import repo_config

    saved = os.environ.get("SPREADSHEETSMITH_CONFIG_DIR")
    os.environ["SPREADSHEETSMITH_CONFIG_DIR"] = tempfile.mkdtemp()  # no config at all
    try:
        check(repo_config.s3_bucket() is None, "s3_bucket() must not invent a name")
        try:
            repo_config.require_s3_bucket()
            check(False, "require_s3_bucket() should raise without a config")
        except EnvironmentError as e:
            check(
                "offline path" in str(e),
                f"the refusal should point at the offline path, got: {e}",
            )
    finally:
        if saved is None:
            os.environ.pop("SPREADSHEETSMITH_CONFIG_DIR", None)
        else:
            os.environ["SPREADSHEETSMITH_CONFIG_DIR"] = saved


def test_v1_has_no_bundle(local_store):
    # With the roots pinned by the environment (as this test file does) every
    # benchmark resolves; the refusal is about the preset, so drop them first.
    saved = {k: os.environ.pop(k) for k in
             (local_store.DATA_ROOT_ENV, local_store.OUTPUT_ROOT_ENV)
             if k in os.environ}
    try:
        local_store.data_root("v1")
        check(False, "v1 should have no offline bundle")
    except local_store.LocalStoreError as e:
        check(
            "not bundled" in str(e),
            f"the v1 refusal should say so plainly, got: {e}",
        )
    finally:
        os.environ.update(saved)


def test_duplicate_attempt_id_refused(local_store, root: Path):
    """The same id in two files is an error, not a silent winner."""
    out = root / "outputs" / "another_agent"
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / local_store.ATTEMPTS_FILENAME
    jsonl.write_text(
        json.dumps({"id": ATTEMPT_ID, "task_id": TASK_ID, "attempt_files": []}) + "\n"
    )
    try:
        local_store.attempt_index()
        check(False, "a duplicate attempt id should raise")
    except local_store.LocalStoreError as e:
        check(str(ATTEMPT_ID) in str(e), f"the error should name the id, got: {e}")
    finally:
        shutil.rmtree(out)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="offline_source_sink_"))
    root = tmp / "repo"
    (root / "outputs").mkdir(parents=True)
    manifest = build_bundle(root)

    os.environ["SPREADSHEETSMITH_DATA_ROOT"] = str(root / "data")
    os.environ["SPREADSHEETSMITH_OUTPUT_ROOT"] = str(root / "outputs")

    import importlib.util

    from utils import local_store
    from utils.misc_utils import load_project_configs

    # build_grading_row stamps the rubric/weight versions from the environment.
    load_project_configs(benchmark="v2")

    spec = importlib.util.spec_from_file_location(
        "gfd_under_test", JUDGE / "main_scripts" / "grade_from_db.py"
    )
    gfd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gfd)

    try:
        test_paths_resolve_to_manifest_hashes(local_store, root, manifest)
        test_task_folder_layout_matches_s3(local_store, gfd, root, tmp)
        test_grading_row_columns(local_store, gfd, tmp)
        test_import_without_cloud()
        test_selection_rule(gfd)
        test_run_config_overrides(gfd)
        test_no_bucket_refusal(local_store)
        test_v1_has_no_bundle(local_store)
        test_duplicate_attempt_id_refused(local_store, root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("OK" if not FAILS else f"{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
