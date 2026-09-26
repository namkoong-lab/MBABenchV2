"""Offline bundle parity: the bundle source, the local sink and the runner's
prompt path agree with the postgres_s3 code path. No network, no database,
no browser.

The `tasks` row for task 1 was saved once from the database (one read-only
SELECT) under tests/fixtures/, with its object-store URIs reduced to keys so
the fixture names no bucket; the test rebuilds the URIs with a placeholder
bucket and feeds the row through SpreadsheetSmithPostgresS3TaskSource's own
row-to-spec path with the S3 download replaced by a copy out of the bundle.

Run from gui-agents-master:  python -m pytest tests/test_offline_bundle.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]  # gui-agents-master
MONOREPO = REPO.parent
sys.path.insert(0, str(REPO))

from claude_web_agent.claude_web_engine import (  # noqa: E402
    create_run_directory,
    resolve_prompts,
)
from infra.configs import load_configs, resolve_agent_identity  # noqa: E402
from infra.run import (  # noqa: E402
    _write_prompts_file,
    build_engine_config,
    collect_log_files,
    preflight_check,
)
from task_io.base import AttemptResult, TaskSpec  # noqa: E402
from task_io.registry import (  # noqa: E402
    apply_offline_fallback,
    build_sink,
    build_source,
)
from task_io.sinks.attempt_row import (  # noqa: E402
    TASK_ATTEMPTS_COLUMNS,
    TASK_ATTEMPTS_INSERT_COLUMNS,
    attempt_row,
)
from task_io.sinks.local_sink import LocalAttemptSink  # noqa: E402
from task_io.sources.bundle_source import BundleTaskSource  # noqa: E402

DEFAULT_PATH = REPO / "infra/configs/configs.default.yaml"
NO_OVERRIDES = REPO / "infra/configs/configs.yaml.absent-on-purpose"
FIXTURE = REPO / "tests/fixtures/tasks_row_task_1.json"
DATA_ROOT = MONOREPO / "data"
TASK1_JSON = DATA_ROOT / "tasks" / "task_id=1" / "task.json"
HOUSE_STANDARDS = (MONOREPO / "house_standards" / "House_Standards_v1.md").resolve()
OFFLINE_DIR = REPO / "infra/configs/run_configs/offline"
FABLE_CONFIG = OFFLINE_DIR / "claude_fable_5_1_cowork_max.yaml"
# The cohort configs; smoke_*.yaml are the pipeline-test variants (prompt version 0) and are not cohorts.
OFFLINE_CONFIGS = sorted(p for p in OFFLINE_DIR.glob("*.yaml") if not p.name.startswith("smoke_"))

LABEL = "claude_fable_5_1_cowork_max"
PLACEHOLDER_BUCKET = "bucket"

# The exact task_attempts column set an offline row must carry.
EXPECTED_COLUMNS = [
    "id",
    "task_id",
    "agent_model_name",
    "agent_model_type",
    "attempt_files",
    "prompt_files",
    "start_time",
    "end_time",
    "time_taken_min",
    "cost",
    "prompt_version",
    "agent_failed",
    "agent_failed_reason",
    "deprecated",
    "created_at",
    "context_reduced",
    "deprecated_reason",
    "updated_at",
    "extra_configs",
]

needs_bundle = pytest.mark.skipif(
    not TASK1_JSON.exists(), reason="offline bundle (data/tasks/) not present"
)
# The task workbooks are not tracked; scripts/install_task_files.py unpacks them
# from the downloaded zip. Tests that open or copy one skip until then.
TASK1_WORKBOOK = DATA_ROOT / "tasks" / "task_id=1" / "starting_files" / "ApfelInc.xlsx"
needs_workbooks = pytest.mark.skipif(
    not TASK1_WORKBOOK.is_file(),
    reason="task workbooks not installed — see README 'Task files' (scripts/install_task_files.py)",
)


# --- helpers ----------------------------------------------------------------


def _load(run_config: dict):
    return load_configs(
        default_path=DEFAULT_PATH, override_path=NO_OVERRIDES, run_config_data=run_config
    )


def _fable_cfg(**extra):
    data = yaml.safe_load(FABLE_CONFIG.read_text())
    data.update(extra)
    return _load(data)


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bundle_spec_for_task_1() -> TaskSpec:
    src = BundleTaskSource(
        data_root=DATA_ROOT,
        repo_root=MONOREPO,
        agent_model_name=LABEL,
        prompt_version=205,
        task_ids=[1],
        task_sources=["v2"],
        skip_deprecated=True,
        skip_already_attempted=False,
    )
    (spec,) = list(src.iter_tasks())
    return spec


def fixture_spec_for_task_1(tmp_path: Path, monkeypatch) -> TaskSpec:
    """The fixture row through the postgres source's own iter_tasks(), with
    the SELECT returning the row and the S3 client copying from the bundle."""
    from task_io.sources import postgres_s3 as pg

    row = json.loads(FIXTURE.read_text())
    bundle_row = json.loads(TASK1_JSON.read_text())
    key_to_path = dict(
        zip(bundle_row["s3_keys"]["task_starting_files"], bundle_row["task_starting_files"])
    )

    class FakeS3:
        def download_file(self, bucket, key, dest):
            assert bucket == PLACEHOLDER_BUCKET, bucket
            shutil.copy2(MONOREPO / key_to_path[key], dest)

    class FakeSTS:
        def get_caller_identity(self):
            return {"Account": "000000000000", "Arn": "arn:aws:iam::000000000000:user/test"}

    monkeypatch.setattr(
        pg.boto3, "client", lambda service, **kw: FakeS3() if service == "s3" else FakeSTS()
    )
    # What the SELECT projects (id, task_name, task_starting_files,
    # task_source), URIs rebuilt from the bucket-free keys.
    selected = {
        "id": row["id"],
        "task_name": row["task_name"],
        "task_starting_files": [
            f"s3://{PLACEHOLDER_BUCKET}/{key}" for key in row["task_starting_files"]
        ],
        "task_source": row["task_source"],
    }
    monkeypatch.setattr(
        pg.SpreadsheetSmithPostgresS3TaskSource, "_select_tasks", lambda self: [selected]
    )
    src = pg.SpreadsheetSmithPostgresS3TaskSource(
        db_url="postgresql://user:secret@localhost/placeholder",
        scratch_dir=tmp_path / "scratch",
        agent_model_name=LABEL,
        prompt_version=205,
        task_ids=[1],
        task_sources=["v2"],
        skip_deprecated=True,
        skip_already_attempted=False,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    (spec,) = list(src.iter_tasks())
    return spec


def populate(run_dir: Path) -> None:
    """Everything one attempt leaves behind (same shape as
    test_attempt_files_offline.populate)."""
    create_run_directory(run_dir)
    (run_dir / "logs" / "conversations").mkdir(parents=True, exist_ok=True)
    (run_dir / "solutions" / "20260925_ApfelInc_Solution_Model.xlsx").write_bytes(b"PK\x03\x04")
    (run_dir / "json_logs" / "completion_claude_web_1.json").write_text("{}")
    (run_dir / "logs" / "claude_web_20260925_ApfelInc.log").write_text("log")
    (run_dir / "logs" / "conversations" / "conversation_x.json").write_text("{}")
    (run_dir / "prompts_ApfelInc_20260925_120000.json").write_text("{}")


def result_for(run_dir: Path, status="success") -> AttemptResult:
    started = datetime(2026, 9, 25, 12, 0, 0)
    finished = datetime(2026, 9, 25, 12, 30, 0)
    return AttemptResult(
        task_id="1",
        task_name="ApfelInc",
        agent_model_name=LABEL,
        prompt_version=205,
        status=status,
        solution_file=run_dir / "solutions" / "20260925_ApfelInc_Solution_Model.xlsx",
        log_files=collect_log_files(run_dir),
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_seconds=1800.0,
        prompt_files=[run_dir / "prompts_ApfelInc_20260925_120000.json"],
        extra={"return_code": 0, "task_metadata": {"db_task_id": 1, "task_source": "v2"}},
    )


@contextmanager
def env(**values):
    prev = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- (1) TaskSpec parity ----------------------------------------------------


@needs_workbooks
def test_bundle_spec_matches_postgres_code_path(tmp_path, monkeypatch):
    b = bundle_spec_for_task_1()
    f = fixture_spec_for_task_1(tmp_path, monkeypatch)

    assert b.task_id == f.task_id == "1"
    assert b.task_name == f.task_name == "ApfelInc"
    assert b.solution_name is None and f.solution_name is None
    assert [p.name for p in b.upload_files] == [p.name for p in f.upload_files]
    assert [_sha(p) for p in b.upload_files] == [_sha(p) for p in f.upload_files]
    # The bundle serves the file in place; the postgres path copied it into
    # the scratch layout. Same name, same bytes, different directory.
    assert b.upload_files[0].parent == DATA_ROOT / "tasks" / "task_id=1" / "starting_files"
    assert f.upload_files[0].parent == tmp_path / "scratch" / "gui" / "task_id=1" / "starting_files"

    assert set(b.metadata) == set(f.metadata) == {
        "source_kind", "db_task_id", "overrides", "task_source",
    }
    strip = lambda m: {k: v for k, v in m.items() if k != "source_kind"}  # noqa: E731
    assert strip(b.metadata) == strip(f.metadata) == {
        "db_task_id": 1, "overrides": {}, "task_source": "v2",
    }
    # The one deliberate difference: the spec says where it came from.
    assert (b.metadata["source_kind"], f.metadata["source_kind"]) == ("bundle", "postgres_s3")


@needs_bundle
def test_fixture_row_matches_bundle_row():
    """The saved database row and the bundled task.json are the same row."""
    fixture = json.loads(FIXTURE.read_text())
    bundle = json.loads(TASK1_JSON.read_text())
    for col, value in fixture.items():
        if col in ("task_starting_files", "task_solution_files"):
            assert value == bundle["s3_keys"][col], col
        else:
            assert value == bundle[col], col
    text = FIXTURE.read_text()
    assert "s3://" not in text


# --- (2) prompt payload parity ---------------------------------------------


@needs_workbooks
def test_prompt_payload_identical(tmp_path, monkeypatch):
    cfg = _fable_cfg()
    ident = resolve_agent_identity(cfg)
    assert ident.model_name == LABEL
    ec_b = build_engine_config(cfg, bundle_spec_for_task_1(), ident.agent_folder)
    ec_f = build_engine_config(
        cfg, fixture_spec_for_task_1(tmp_path, monkeypatch), ident.agent_folder
    )
    assert ec_b["prompt_version"] == ec_f["prompt_version"] == 205
    assert ec_b["prompts_file"] == ec_f["prompts_file"] == ["tasks_configs/prompts/v2_3.txt"]

    started = datetime(2026, 9, 25, 12, 0, 0)
    path_b = _write_prompts_file(tmp_path / "b", "ApfelInc", ec_b, started)
    path_f = _write_prompts_file(tmp_path / "f", "ApfelInc", ec_f, started)
    assert path_b.name == path_f.name == "prompts_ApfelInc_20260925_120000.json"
    assert path_b.read_bytes() == path_f.read_bytes()

    payload = json.loads(path_b.read_text())
    assert payload["prompt_version"] == 205
    assert len(payload["prompts"]) == 1
    assert resolve_prompts(dict(ec_b))["prompts"] == resolve_prompts(dict(ec_f))["prompts"]
    (att,) = payload["attachments"]
    assert att["name"] == "House_Standards_v1.md"
    assert att["sha256"] == _sha(HOUSE_STANDARDS)


# --- (3) attachment list parity --------------------------------------------


@needs_workbooks
def test_attachment_list_identical(tmp_path, monkeypatch):
    cfg = _fable_cfg()
    ident = resolve_agent_identity(cfg)
    ec_b = build_engine_config(cfg, bundle_spec_for_task_1(), ident.agent_folder)
    ec_f = build_engine_config(
        cfg, fixture_spec_for_task_1(tmp_path, monkeypatch), ident.agent_folder
    )
    names_b = [Path(u).name for u in ec_b["upload_files"]]
    names_f = [Path(u).name for u in ec_f["upload_files"]]
    assert names_b == names_f == ["ApfelInc.xlsx", "House_Standards_v1.md"]
    assert ec_b["prompt_attachments"] == ec_f["prompt_attachments"] == [str(HOUSE_STANDARDS)]
    assert [_sha(u) for u in ec_b["upload_files"]] == [_sha(u) for u in ec_f["upload_files"]]
    assert not preflight_check(ec_b, "claude", "v2")
    assert not preflight_check(ec_f, "claude", "v2")


# --- (4) the local sink's row ----------------------------------------------


def test_local_sink_row_has_exactly_the_task_attempts_columns(tmp_path):
    run_dir = tmp_path / "staging" / "attempt"
    populate(run_dir)
    sink = LocalAttemptSink(
        output_root=tmp_path / "outputs",
        agent_model_name=LABEL,
        agent_model_type="gui",
        prompt_version=205,
        repo_root=tmp_path,
    )
    assert sink.retains_files is True
    before = int(time.time() * 1000)
    sink.publish(result_for(run_dir))
    after = int(time.time() * 1000)

    log = tmp_path / "outputs" / LABEL / "task_attempts.jsonl"
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])

    assert set(row) == set(EXPECTED_COLUMNS)
    assert list(row) == EXPECTED_COLUMNS == list(TASK_ATTEMPTS_COLUMNS)
    assert before <= row["id"] <= after
    assert row["task_id"] == 1
    assert row["agent_model_name"] == LABEL
    assert row["agent_model_type"] == "gui"
    assert row["prompt_version"] == 205
    assert row["agent_failed"] is False and row["agent_failed_reason"] is None
    assert row["deprecated"] is False and row["cost"] is None
    assert row["time_taken_min"] == 30.0
    assert (row["context_reduced"], row["deprecated_reason"], row["updated_at"], row["extra_configs"]) == (None, None, None, None)
    for col in ("start_time", "end_time", "created_at"):
        assert datetime.fromisoformat(row[col]).tzinfo is not None, col

    # Every path is repo-relative POSIX and exists; the workbook comes first.
    paths = row["attempt_files"] + row["prompt_files"]
    assert paths, row
    for rel in paths:
        assert not rel.startswith("/") and "\\" not in rel, rel
        assert (tmp_path / rel).is_file(), rel
    assert row["attempt_files"][0].endswith(".xlsx")
    assert [Path(p).name for p in row["attempt_files"]] == [
        "20260925_ApfelInc_Solution_Model.xlsx",
        "completion_claude_web_1.json",
        "conversation_x.json",
        "claude_web_20260925_ApfelInc.log",
    ]
    assert [Path(p).name for p in row["prompt_files"]] == ["prompts_ApfelInc_20260925_120000.json"]
    folders = {str(Path(p).parent) for p in paths}
    assert len(folders) == 1
    (folder,) = folders
    parent, stamp = folder.rsplit("/", 1)
    assert parent == f"outputs/{LABEL}/task_id=1"
    assert len(stamp) == 15 and stamp[8] == "_" and stamp.replace("_", "").isdigit()
    # The old attempts.ndjson line is gone.
    assert not (tmp_path / "outputs" / LABEL / "attempts.ndjson").exists()
    assert not (tmp_path / "outputs" / "attempts.ndjson").exists()


def test_local_sink_failed_attempt_row(tmp_path):
    run_dir = tmp_path / "staging" / "attempt"
    populate(run_dir)
    (run_dir / "solutions" / "20260925_ApfelInc_Solution_Model.xlsx").unlink()
    sink = LocalAttemptSink(
        output_root=tmp_path / "outputs", agent_model_name=LABEL, prompt_version=205,
        repo_root=tmp_path,
    )
    r = result_for(run_dir, status="failed")
    r.solution_file = None
    r.extra["failure_reason"] = "no solution workbook was produced"
    sink.publish(r)
    row = json.loads((tmp_path / "outputs" / LABEL / "task_attempts.jsonl").read_text())
    assert row["agent_failed"] is True
    assert row["agent_failed_reason"] == "no solution workbook was produced"
    assert not row["attempt_files"][0].endswith(".xlsx")
    assert set(row) == set(EXPECTED_COLUMNS)


def test_both_sinks_build_the_row_with_one_function(tmp_path):
    from task_io.sinks.postgres_s3 import (
        TASK_ATTEMPTS_SCHEMA,
        SpreadsheetSmithPostgresS3AttemptSink,
    )

    assert TASK_ATTEMPTS_SCHEMA.columns == TASK_ATTEMPTS_INSERT_COLUMNS
    assert set(TASK_ATTEMPTS_INSERT_COLUMNS) | {
        "id", "created_at", "context_reduced", "deprecated_reason", "updated_at", "extra_configs",
    } == set(EXPECTED_COLUMNS)

    run_dir = tmp_path / "attempt"
    populate(run_dir)
    result = result_for(run_dir)
    pg = SpreadsheetSmithPostgresS3AttemptSink.__new__(SpreadsheetSmithPostgresS3AttemptSink)
    pg.agent_model_name, pg.agent_model_type, pg.prompt_version = LABEL, "gui", 205
    via_sink = pg._attempt_values(result, ["s3://b/x.xlsx"], ["s3://b/p.json"])
    direct = attempt_row(
        result, agent_model_name=LABEL, agent_model_type="gui", prompt_version=205,
        attempt_files=["s3://b/x.xlsx"], prompt_files=["s3://b/p.json"],
    )
    assert via_sink == direct
    assert tuple(via_sink) == TASK_ATTEMPTS_INSERT_COLUMNS


# --- skip_already_attempted against the local log ---------------------------


@needs_workbooks
def test_skip_already_attempted_reads_the_local_log(tmp_path):
    log = tmp_path / "outputs" / LABEL / "task_attempts.jsonl"
    log.parent.mkdir(parents=True)

    def ids(**kw):
        src = BundleTaskSource(
            data_root=DATA_ROOT, repo_root=MONOREPO, agent_model_name=LABEL,
            prompt_version=205, task_ids=[1, 2, 3], skip_already_attempted=True,
            attempts_log=log, **kw,
        )
        return [int(s.task_id) for s in src.iter_tasks()]

    def row(task_id, **over):
        base = {"task_id": task_id, "agent_model_name": LABEL, "prompt_version": 205,
                "agent_failed": False, "deprecated": False}
        base.update(over)
        return json.dumps(base)

    assert ids() == [1, 2, 3]  # no log yet
    log.write_text("\n".join([
        row(1),                                   # success -> skipped
        row(2, agent_failed=True),                # failed row does not count
        row(3, deprecated=True),                  # nor a deprecated one
    ]) + "\n")
    assert ids() == [2, 3]
    log.write_text("\n".join([
        row(2, prompt_version=204),               # another prompt version
        row(3, agent_model_name="someone_else"),  # another cohort
    ]) + "\n")
    assert ids() == [1, 2, 3]


# --- the whole bundle -------------------------------------------------------


@needs_workbooks
def test_bundle_yields_all_101_tasks_in_id_order():
    src = BundleTaskSource(
        data_root=DATA_ROOT, repo_root=MONOREPO, agent_model_name=LABEL,
        prompt_version=205, task_sources=["v2"], skip_already_attempted=False,
    )
    specs = list(src.iter_tasks())
    assert [int(s.task_id) for s in specs] == list(range(1, 102))
    for s in specs:
        row = json.loads((DATA_ROOT / "tasks" / f"task_id={s.task_id}" / "task.json").read_text())
        assert s.task_name == row["task_name"]
        assert all(p.is_file() for p in s.upload_files), s.task_id
        # The names the postgres source would give the downloads (the
        # object-store basenames) are the bundle's names.
        assert [p.name for p in s.upload_files] == [
            Path(k).name for k in row["s3_keys"]["task_starting_files"]
        ], s.task_id
        assert s.metadata["db_task_id"] == int(s.task_id)


# --- selection rule ---------------------------------------------------------


@contextmanager
def no_database_url(tmp_path):
    """A monorepo config with no urls, and no env url."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config_default.yaml").write_text("{}\n")
    (cfg_dir / "config.yaml").write_text("{}\n")
    with env(SPREADSHEETSMITH_CONFIG_DIR=str(cfg_dir), SPREADSHEETSMITHJUDGE_KEYS_DATABASE_URL=None):
        yield


def _cloud_cfg(**extra):
    return _load({
        "benchmark": "v2",
        "source": {"kind": "postgres_s3", "schema": "spreadsheetsmith"},
        "sink": {"kind": "postgres_s3", "schema": "spreadsheetsmith"},
        "provider": {"kind": "claude"},
        "claude_web": {"mode": "cowork", "model": "fable_5_1", "effort": "max"},
        **extra,
    })


def test_no_url_switches_postgres_s3_to_bundle_and_local(tmp_path):
    with no_database_url(tmp_path):
        cfg = _cloud_cfg()
        note = apply_offline_fallback(cfg)
    assert note and "source.kind=postgres_s3 -> bundle" in note and "sink.kind=postgres_s3 -> local" in note
    assert (cfg.source.kind, cfg.source.schema) == ("bundle", None)
    assert (cfg.sink.kind, cfg.sink.schema) == ("local", None)
    assert "postgresql://" not in note


def test_configured_url_is_never_bypassed(tmp_path):
    with no_database_url(tmp_path):
        cfg = _cloud_cfg(database={"url": "postgresql://user:secret@localhost/placeholder"})
        assert apply_offline_fallback(cfg) is None
    assert (cfg.source.kind, cfg.sink.kind) == ("postgres_s3", "postgres_s3")
    with no_database_url(tmp_path):
        with env(SPREADSHEETSMITHJUDGE_KEYS_DATABASE_URL="postgresql://user:secret@localhost/placeholder"):
            cfg = _cloud_cfg()
            assert apply_offline_fallback(cfg) is None
    assert (cfg.source.kind, cfg.sink.kind) == ("postgres_s3", "postgres_s3")


def test_explicit_offline_kinds_are_left_alone(tmp_path):
    with no_database_url(tmp_path):
        cfg = _fable_cfg()
        assert apply_offline_fallback(cfg) is None
        assert (cfg.source.kind, cfg.sink.kind) == ("bundle", "local")
        v1 = _load({"benchmark": "v1", "prompt_version": 9,
                    "source": {"kind": "postgres_s3", "schema": "bizbench"},
                    "sink": {"kind": "postgres_s3", "schema": "bizbench"},
                    "provider": {"kind": "claude"},
                    "claude_web": {"mode": "cowork", "model": "fable_5", "effort": "max"}})
        assert apply_offline_fallback(v1) is None  # no v1 bundle: unchanged
        assert v1.source.kind == "postgres_s3"


# --- the checked-in offline configs ----------------------------------------


def test_four_offline_cohort_configs_exist():
    assert [p.name for p in OFFLINE_CONFIGS] == [
        "chatgpt_gpt_6_astra_work_ultra.yaml",
        "chatgpt_gpt_6_pro.yaml",
        "claude_fable_5_1_cowork_max.yaml",
        "claude_opus_5_cowork_max.yaml",
    ]


@needs_workbooks
@pytest.mark.parametrize("path", OFFLINE_CONFIGS, ids=[p.stem for p in OFFLINE_CONFIGS])
def test_offline_config_builds_bundle_source_and_local_sink(path, tmp_path):
    cfg = _load(yaml.safe_load(path.read_text()))
    assert cfg.benchmark == "v2" and cfg.prompt_version == 205
    assert cfg.source.kind == "bundle" and cfg.sink.kind == "local"
    assert cfg.source.filters.task_ids == list(range(1, 102))
    assert cfg.source.filters.task_sources == ["v2"]
    assert cfg.source.filters.skip_already_attempted is True
    ident = resolve_agent_identity(cfg)
    assert ident.model_name == path.stem, (ident, path)
    cfg.agent.prompt_version = cfg.prompt_version
    with env(SPREADSHEETSMITH_OUTPUT_ROOT=str(tmp_path / "outputs")):
        source = build_source(cfg)
        sink = build_sink(cfg)
    assert isinstance(source, BundleTaskSource)
    assert isinstance(sink, LocalAttemptSink)
    assert source.attempts_log == tmp_path / "outputs" / ident.model_name / "task_attempts.jsonl"
    assert sink.log_path == source.attempts_log
    assert len(list(source.iter_tasks())) == 101
    # Nothing machine-specific: the browser block is the schema default.
    block = getattr(cfg, f"{cfg.provider.kind}_web")
    assert block.browser.cdp_port == 9222
    assert block.browser.profile_dir.startswith("browser_profiles/")


@needs_workbooks
def test_dry_run_task_1_offline(tmp_path):
    """`--dry-run` on the fable config resolves task 1 end to end without a
    browser, a database or object-store credentials."""
    proc = subprocess.run(
        [sys.executable, "-m", "infra.run", "--dry-run", "--task-id", "1",
         "--run-config", str(FABLE_CONFIG.relative_to(REPO))],
        cwd=REPO,
        env={**os.environ, "SPREADSHEETSMITH_OUTPUT_ROOT": str(tmp_path / "outputs")},
        capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert "[DRY RUN] task_id=1 ApfelInc" in out
    assert "tasks_configs/prompts/v2_3.txt" in out
    assert "House_Standards_v1.md" in out
    assert f"outputs/{LABEL}/task_id=1/" in out
    assert "task_attempts.jsonl" in out
    assert not (tmp_path / "outputs" / LABEL / "task_id=1").exists()
