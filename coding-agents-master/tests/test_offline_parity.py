"""Offline parity — the bundled path must produce what the cloud path does.

No Docker, no database, no object store, no API keys. The cloud half is
driven from tests/fixtures/tasks_row_task_1.json (a real `tasks` row, captured
read-only) with a stub download in place of the object store, so both sources
can be run side by side and compared byte for byte.

Run:  python3 tests/test_offline_parity.py   (or pytest tests/)
"""
import json
import os
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding_agent import repo_config  # noqa: E402
from coding_agent.config import BENCHMARKS, TaskRange, load_config, resolve_io  # noqa: E402
from coding_agent.local_sink import attempted_task_ids, write_attempt  # noqa: E402
from coding_agent.prompt_builder import build_prompt  # noqa: E402
from coding_agent.task_source import (InternalSource, LocalSource,  # noqa: E402
                                      local_task_ids)
from coding_agent.workspace import create_attempt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DB_ROW = json.loads((FIXTURES / "tasks_row_task_1.json").read_text())
DB_COLUMNS = json.loads((FIXTURES / "task_attempts_columns.json").read_text())
OFFLINE_CONFIGS = ROOT / "run_configs" / "offline"


def workbook_bytes(tag: str) -> bytes:
    import io

    import openpyxl
    wb = openpyxl.Workbook()
    wb.active["A1"] = tag
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_bundle(tmp: Path, row: dict = DB_ROW, task_id: int = 1) -> Path:
    """A data_root holding one task exactly as the exporter writes it: the
    row verbatim with the file columns rewritten to repo-relative paths and
    the original keys kept under "s3_keys" (data/README.md)."""
    data_root = tmp / "data"
    tdir = data_root / "tasks" / f"task_id={task_id}"
    (tdir / "starting_files").mkdir(parents=True, exist_ok=True)
    rewritten = []
    for uri in row["task_starting_files"]:
        name = Path(uri).name
        (tdir / "starting_files" / name).write_bytes(workbook_bytes(name))
        rewritten.append(f"data/tasks/task_id={task_id}/starting_files/{name}")
    local_row = {**row, "id": task_id, "task_starting_files": rewritten,
                 "s3_keys": {"task_starting_files": list(row["task_starting_files"])}}
    (tdir / "task.json").write_text(json.dumps(local_row, indent=2))
    return data_root


@contextmanager
def data_root_at(path: Path):
    """Point local.data_root at a throwaway tree for the duration."""
    before = os.environ.get(repo_config.DATA_ROOT_ENV)
    os.environ[repo_config.DATA_ROOT_ENV] = str(path)
    try:
        yield
    finally:
        os.environ.pop(repo_config.DATA_ROOT_ENV, None)
        if before is not None:
            os.environ[repo_config.DATA_ROOT_ENV] = before


@contextmanager
def stub_cloud(monkeypatched_bytes):
    """Stand in for the database and the object store: the fixture row, and
    downloads that hand back the same bytes the bundle holds."""
    class _Cursor:
        def __init__(self): self.row = None
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            self.row = (DB_ROW["task_name"], DB_ROW["task_source"],
                        DB_ROW["task_starting_files"], DB_ROW["deprecated"])
        def fetchone(self): return self.row

    class _Conn:
        def cursor(self, *a, **k): return _Cursor()
        def close(self): pass

    class _S3:
        def download_file(self, bucket, key, dest):
            Path(dest).write_bytes(monkeypatched_bytes[Path(key).name])

    import coding_agent.task_source as ts
    fake_pg = types.ModuleType("psycopg2")
    fake_pg.connect = lambda *a, **k: _Conn()
    real_pg, real_s3 = sys.modules.get("psycopg2"), ts.s3_client
    sys.modules["psycopg2"] = fake_pg
    ts.s3_client = lambda: _S3()
    try:
        yield
    finally:
        ts.s3_client = real_s3
        if real_pg is not None:
            sys.modules["psycopg2"] = real_pg
        else:
            sys.modules.pop("psycopg2", None)


def test_local_source_matches_the_database_row():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_root = make_bundle(tmp)
        with data_root_at(data_root):
            spec = LocalSource(1, data_root).fetch(tmp / "staging_local")

        names = [Path(u).name for u in DB_ROW["task_starting_files"]]
        assert spec.task_id == DB_ROW["id"]
        assert spec.task_name == DB_ROW["task_name"]
        assert spec.task_source == DB_ROW["task_source"]
        # Same file names, in the column's order — the S3 path's contract.
        assert [p.name for p in spec.starting_files] == names
        # The bundled row keeps the original keys, so the rewriting is reversible.
        bundled = json.loads((data_root / "tasks" / "task_id=1" / "task.json").read_text())
        assert bundled["s3_keys"]["task_starting_files"] == DB_ROW["task_starting_files"]
        for column in DB_ROW:
            if column != "task_starting_files":
                assert bundled[column] == DB_ROW[column], column
        print("ok: local source yields the database row after path rewriting")


def test_prompt_and_staged_files_are_byte_identical():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_root = make_bundle(tmp)
        payload = {Path(u).name: workbook_bytes(Path(u).name)
                   for u in DB_ROW["task_starting_files"]}
        cfg = load_config(OFFLINE_CONFIGS / "claudecode_anthropic__claude-fable-5-1-max.yaml")

        built = {}
        for label in ("local", "cloud"):
            cfg.workspaces_dir = tmp / f"ws_{label}"
            cfg.workspaces_dir.mkdir()
            if label == "local":
                with data_root_at(data_root):
                    spec = LocalSource(1, data_root).fetch(tmp / "staging_local")
            else:
                with stub_cloud(payload):
                    spec = InternalSource(1, "postgresql://stub").fetch(tmp / "staging_cloud")
            attempt = create_attempt(cfg.workspaces_dir, spec)
            build_prompt(cfg, spec, attempt.workspace, attempt=attempt)
            built[label] = attempt

        local, cloud = built["local"], built["cloud"]
        assert (local.workspace / "PROMPT.md").read_bytes() == \
               (cloud.workspace / "PROMPT.md").read_bytes()
        # Same staged names, same order, same content hashes (the manifest).
        assert sorted(p.name for p in (local.workspace / "starting_files").iterdir()) == \
               sorted(p.name for p in (cloud.workspace / "starting_files").iterdir())
        assert local.manifest == cloud.manifest
        # v13 stages the house standards into the workspace root.
        assert "HOUSE_STANDARDS.md" in local.manifest
        print("ok: PROMPT.md and the staged file set are byte-identical")


def test_local_sink_row_has_exactly_the_database_columns():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        from datetime import datetime
        attempt_dir = tmp / "attempt"
        workspace = attempt_dir / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "PROMPT.md").write_text("prompt\n")
        (attempt_dir / "transcript.jsonl").write_text("{}\n")
        (attempt_dir / "verdict.json").write_text("{}\n")
        solution = workspace / "solution.xlsx"
        solution.write_bytes(workbook_bytes("solution"))
        prompt_src = tmp / "system_prompt_coding_v1.txt"
        prompt_src.write_text("system\n")

        started = datetime.now()
        row, dest = write_attempt(
            output_root=tmp / "outputs",
            agent_model_name="codex_tensorblock/glm-5.3-max",  # a label with a slash
            task_id=1, started_at=started, ended_at=datetime.now(),
            solution_path=solution, workspace=workspace, attempt_dir=attempt_dir,
            prompt_paths=[prompt_src], time_taken_min=12.5, cost=1.25,
            prompt_version=113, agent_failed=False, agent_failed_reason=None,
            extra_configs={"cli": "codex"})

        assert list(row) == DB_COLUMNS, f"{set(row) ^ set(DB_COLUMNS)}"
        assert row["agent_model_type"] == "coding_cli"
        assert row["id"] > 10 ** 12  # millisecond epoch, never a database id
        # solution.xlsx first: the judge grades the first xlsx in the list.
        assert Path(row["attempt_files"][0]).name == "solution.xlsx"
        assert [Path(p).name for p in row["attempt_files"]] == \
               ["solution.xlsx", "PROMPT.md", "transcript.jsonl", "verdict.json"]
        assert [Path(p).name for p in row["prompt_files"]] == ["system_prompt_coding_v1.txt"]
        for stamp in ("start_time", "end_time", "created_at", "updated_at"):
            assert row[stamp][-6] in "+-", f"{stamp} needs an ISO-8601 offset: {row[stamp]}"
        # The label's slash nests one directory; the row is one line of JSONL.
        jsonl = tmp / "outputs" / "codex_tensorblock" / "glm-5.3-max" / "task_attempts.jsonl"
        assert json.loads(jsonl.read_text().strip()) == row
        assert dest.parent.name == "task_id=1"
        assert attempted_task_ids(tmp / "outputs", "codex_tensorblock/glm-5.3-max", 113) == {1}
        assert attempted_task_ids(tmp / "outputs", "codex_tensorblock/glm-5.3-max", 114) == set()
        print("ok: local sink row carries exactly the task_attempts columns")


def test_task_listing_applies_the_same_filters():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_root = make_bundle(tmp, task_id=1)
        make_bundle(tmp, {**DB_ROW, "deprecated": True}, task_id=2)
        make_bundle(tmp, {**DB_ROW, "task_source": "fmwc"}, task_id=3)
        make_bundle(tmp, task_id=4)
        assert local_task_ids(data_root) == [1, 4]
        assert local_task_ids(data_root, first=2) == [4]
        assert local_task_ids(data_root, last=3) == [1]
        assert local_task_ids(data_root, task_source="fmwc") == [3]

        # tasks.json (the whole table in one file) is read in preference to
        # the per-task files and must give the same answer.
        rows = [json.loads(p.read_text())
                for p in sorted((data_root / "tasks").glob("task_id=*/task.json"))]
        (data_root / "tasks" / "tasks.json").write_text(json.dumps(rows))
        assert local_task_ids(data_root) == [1, 4]
        assert local_task_ids(data_root, first=4) == [4]
        print("ok: bundled task listing drops deprecated rows and other sources")


def test_offline_selection_rule():
    with tempfile.TemporaryDirectory() as td:
        empty_config_dir = Path(td) / "no_config"
        empty_config_dir.mkdir()
        cfg = load_config(OFFLINE_CONFIGS / "codex_openai__gpt-6-astra-xhigh.yaml")
        resolve_io(cfg)
        assert (cfg.io_source, cfg.io_sink) == ("local", "local")
        assert "run config" in cfg.io_reason

        # auto + no URL anywhere -> local, and the log says why.
        auto = load_config(ROOT / "run_configs" / "example_v2_claude.yaml")
        before_cfg = os.environ.get(repo_config.REPO_CONFIG_DIR_ENV)
        before_url = os.environ.pop("DATABASE_URL", None)
        os.environ[repo_config.REPO_CONFIG_DIR_ENV] = str(empty_config_dir)
        try:
            resolve_io(auto)
            assert (auto.io_source, auto.io_sink) == ("local", "local")
            assert "resolved to nothing" in auto.io_reason
            # auto + a URL -> cloud: never go offline behind a configured database.
            os.environ["DATABASE_URL"] = "postgresql://stub/SpreadsheetSmith"
            configured = load_config(ROOT / "run_configs" / "example_v2_claude.yaml")
            resolve_io(configured)
            assert (configured.io_source, configured.io_sink) == ("cloud", "cloud")
        finally:
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop(repo_config.REPO_CONFIG_DIR_ENV, None)
            if before_cfg is not None:
                os.environ[repo_config.REPO_CONFIG_DIR_ENV] = before_cfg
            if before_url is not None:
                os.environ["DATABASE_URL"] = before_url
        print("ok: local is chosen explicitly, or only when no database URL resolves")


def test_v1_has_no_bundle():
    cfg = load_config(ROOT / "run_configs" / "example_fable.yaml")  # benchmark v1
    assert BENCHMARKS["v1"]["data_root"] is None
    try:
        cfg.local_data_root
    except ValueError as e:
        assert "not bundled" in str(e)
    else:
        raise AssertionError("v1 must refuse the offline source")
    print("ok: v1 inputs are not bundled")


def test_offline_run_configs_are_complete():
    expected = {
        ("claudecode_anthropic/claude-fable-5-1-max", "v13"),
        ("claudecode_anthropic/claude-fable-5-1-high", "v13"),
        ("claudecode_anthropic/claude-fable-5-1-low", "v13"),
        ("claudecode_anthropic/claude-opus-5-max", "v13"),
        ("codex_openai/gpt-6-astra-xhigh", "v13"),
        ("codex_tensorblock/gemini-3.8-flash-high", "v13"),
        ("codex_tensorblock/grok-4.6-xhigh", "v13"),
        ("codex_tensorblock/kimi-k3-max", "v13"),
        ("codex_tensorblock/qwen3.8-max-xhigh", "v13"),
        ("codex_tensorblock/glm-5.3-max", "v13"),
        ("codex_tensorblock/claude-opus-5-max", "v13"),
        ("codex_tensorblock/claude-fable-5-1-max", "v14"),
        ("codex_tensorblock/claude-fable-5-1-max", "v15"),
    }
    seen = set()
    for path in sorted(OFFLINE_CONFIGS.glob("*.yaml")):
        cfg = load_config(path)
        assert cfg.benchmark == "v2" and cfg.mode == "internal", path.name
        assert (cfg.source, cfg.sink) == ("local", "local"), path.name
        assert cfg.tasks == TaskRange(1, 101), path.name
        assert path.stem.startswith(cfg.agent_model_name.replace("/", "__")), path.name
        seen.add((cfg.agent_model_name, cfg.template_version))
    assert seen == expected, seen ^ expected
    print(f"ok: {len(seen)} offline run configs, one per cohort")


def _skip(reason: str) -> None:
    """A pytest skip under pytest; a printed SKIP line when run as a script."""
    if "pytest" in sys.modules:
        sys.modules["pytest"].skip(reason)
    print(f"SKIP {reason}")


def test_the_shipped_bundle_matches_the_database_row():
    """The bundle in the repository, not a synthetic one: task 1's row is the
    database row with the file columns rewritten, and the files it names are
    there. Skipped on a checkout whose data/ has not been unpacked."""
    from coding_agent import repo_config
    bundle = repo_config.data_root() / "tasks" / "task_id=1" / "task.json"
    if not bundle.exists():
        print("SKIP shipped bundle not present")
        return
    # The workbooks are not tracked; scripts/install_task_files.py unpacks them.
    workbook = repo_config.data_root() / "tasks" / "task_id=1" / "starting_files" / "ApfelInc.xlsx"
    if not workbook.is_file():
        _skip("task workbooks not installed — see README 'Task files' (scripts/install_task_files.py)")
        return
    row = json.loads(bundle.read_text())
    for column, value in DB_ROW.items():
        if column in ("task_starting_files", "task_solution_files"):
            continue
        assert row[column] == value, column
    for column in ("task_starting_files", "task_solution_files"):
        assert row[column], column
        for rel in row[column]:
            assert not rel.startswith("s3://"), rel
            assert repo_config.bundled_path(rel).is_file(), rel
        assert len(row["s3_keys"][column]) == len(row[column]), column

    spec = LocalSource(1, repo_config.data_root()).fetch(Path(tempfile.mkdtemp()) / "staging")
    assert spec.task_name == DB_ROW["task_name"]
    assert [p.name for p in spec.starting_files] == \
           [Path(u).name for u in DB_ROW["task_starting_files"]]
    print("ok: the shipped bundle reproduces the database row for task 1")


def test_extra_configs_matches_a_recorded_row():
    """What the local sink stores in extra_configs is what the cloud sink
    stored: same keys, same identity settings, for the same cohort."""
    from coding_agent import repo_config
    from coding_agent.recorder import _extras_stamp
    results = repo_config.data_root() / "results" / "task_attempts.jsonl"
    if not results.exists():
        print("SKIP bundled results not present")
        return
    cfg = load_config(OFFLINE_CONFIGS / "claudecode_anthropic__claude-fable-5-1-max.yaml")
    recorded = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    rows = [r for r in recorded
            if r.get("agent_model_name") == cfg.agent_model_name
            and r.get("prompt_version") == 113 and r.get("extra_configs")]
    assert rows, "no recorded rows for this cohort in the bundle"
    ours = {**cfg.extra_configs(), **_extras_stamp(cfg)}
    theirs = rows[-1]["extra_configs"]
    assert set(ours) == set(theirs), set(ours) ^ set(theirs)
    for key in ("cli", "model", "effort", "extra_args", "env", "sandbox_image",
                "house_standards", "prompt_extras", "harness_defaults"):
        assert ours[key] == theirs[key], key
    print("ok: extra_configs matches a recorded row of the same cohort")


if __name__ == "__main__":
    test_local_source_matches_the_database_row()
    test_prompt_and_staged_files_are_byte_identical()
    test_local_sink_row_has_exactly_the_database_columns()
    test_task_listing_applies_the_same_filters()
    test_offline_selection_rule()
    test_v1_has_no_bundle()
    test_offline_run_configs_are_complete()
    test_the_shipped_bundle_matches_the_database_row()
    test_extra_configs_matches_a_recorded_row()
    print("ALL OFFLINE PARITY TESTS PASSED")
