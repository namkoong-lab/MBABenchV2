"""Offline parity: the bundle source + local sink read and write exactly what
the postgres_s3 pair does, with no database, S3, browser or network.

Fixture: tests/fixtures/tasks_row_task_1.json is the `tasks` row for task 1
as one read-only SELECT returned it (bucket name replaced by <bucket>). It
is fed through the cloud source's own row-to-spec code with the S3 download
replaced by a copy from the bundle, and compared with what the bundle
source yields for the same task.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
import yaml
from infra.configs import load_configs, resolve_agent_identity
from infra.configs.prompt_registry import (
    resolve_prompt_attachments,
    resolve_prompt_files,
)
from infra.run import (
    _load_prompt_texts,
    _write_prompts_file,
    attachment_records,
    build_engine_config,
    house_standards_stamp,
    preflight_check,
)
from task_io import registry
from task_io.base import AttemptResult
from task_io.local_layout import OUTPUT_PREFIX, attempts_file, output_path
from task_io.registry import (
    apply_offline_fallback,
    build_sink,
    build_source,
    explicit_io_kinds,
)
from task_io.sinks.attempt_row import TASK_ATTEMPTS_COLUMNS, attempt_row
from task_io.sinks.local_sink import LocalAttemptSink, _with_offset
from task_io.sinks.postgres_s3 import (
    TASK_ATTEMPTS_SCHEMA,
    SpreadsheetSmithPostgresS3AttemptSink,
)
from task_io.sources.bundle_source import BundleTaskSource
from task_io.sources.postgres_s3 import SpreadsheetSmithPostgresS3TaskSource

MEMBER_ROOT = Path(__file__).resolve().parents[1]
MONOREPO_ROOT = MEMBER_ROOT.parent
DATA_ROOT = MONOREPO_ROOT / "data"
TASK_1_DIR = DATA_ROOT / "tasks" / "task_id=1"
FIXTURE = MEMBER_ROOT / "tests" / "fixtures" / "tasks_row_task_1.json"
OFFLINE_CONFIGS = MEMBER_ROOT / "infra" / "configs" / "run_configs" / "offline"
FABLE_CONFIG = OFFLINE_CONFIGS / "claude_excel_fable_5_1.yaml"
HOUSE_STANDARDS = MONOREPO_ROOT / "house_standards" / "House_Standards_v1.md"
LABEL = "claude_excel_fable_5_1"

pytestmark = pytest.mark.skipif(
    not (TASK_1_DIR / "task.json").is_file(),
    reason="offline bundle (data/tasks/task_id=1) not present",
)
# The task workbooks are not tracked; scripts/install_task_files.py unpacks them
# from the downloaded zip. Tests that open or copy one skip until then.
TASK_1_WORKBOOK = TASK_1_DIR / "starting_files" / "ApfelInc.xlsx"
needs_workbooks = pytest.mark.skipif(
    not TASK_1_WORKBOOK.is_file(),
    reason="task workbooks not installed — see README 'Task files' (scripts/install_task_files.py)",
)

# The extra_configs keys every real excel row carries (data/results).
EXCEL_EXTRA_CONFIGS_KEYS = {
    "cdp_port", "provider", "infra_tries", "agent_folder", "ui_model_label",
    "house_standards", "thinking_effort", "agent_model_type", "engine_task_status",
}


# ---- helpers ---------------------------------------------------------------


def _load_run_config(tmp_path: Path, path: Path = FABLE_CONFIG):
    cfg = load_configs(override_path=tmp_path / "absent.yaml", run_config_path=path)
    cfg.agent.prompt_version = cfg.prompt_version  # what infra.run.main does
    return cfg


def _bundle_spec(tmp_path: Path):
    source = BundleTaskSource(
        data_root=DATA_ROOT,
        output_root=tmp_path / "outputs",
        agent_model_name=LABEL,
        prompt_version=205,
        task_ids=[1],
        skip_already_attempted=False,
    )
    specs = list(source.iter_tasks())
    assert len(specs) == 1
    return specs[0]


class _FakeS3:
    """boto3 stand-in: `download_file` copies the object from the bundle."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        self.calls.append((bucket, key))
        assert bucket == "<bucket>"  # the fixture is free of the real name
        shutil.copy2(TASK_1_DIR / "starting_files" / Path(key).name, dest)


class _FixtureSource(SpreadsheetSmithPostgresS3TaskSource):
    """The cloud source with only its I/O replaced: the DB SELECT returns the
    saved row and the S3 client is _FakeS3. `iter_tasks`, `_metadata_for`
    and `_download_starting_files` are the real ones."""

    def __init__(self, row: dict, scratch_dir: Path):
        # PostgresS3TaskSource.__init__ builds boto3 clients and runs an STS
        # preflight eagerly, so its attributes are set here by hand.
        self.db_url = "postgresql://fixture"
        self.scratch_dir = Path(scratch_dir)
        self.task_schema = self.TASK_SCHEMA
        self.task_ids = [1]
        self._conn = None
        self._s3 = _FakeS3()
        self._sts = None
        self.agent_model_name = LABEL
        self.prompt_version = 205
        self.task_sources = []
        self.skip_deprecated = True
        self.skip_already_attempted = False
        self._row = row

    def _select_tasks(self) -> list[dict]:
        return [dict(self._row)]


def _fixture_spec(tmp_path: Path):
    row = json.loads(FIXTURE.read_text())
    source = _FixtureSource(row, tmp_path / "scratch")
    specs = list(source.iter_tasks())
    assert len(specs) == 1
    assert source._s3.calls, "the cloud path must have gone through the S3 download"
    return specs[0]


def _sha(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _prompt_context(cfg):
    identity = resolve_agent_identity(cfg)
    prompt_files = resolve_prompt_files(cfg)
    prompt_texts = _load_prompt_texts(prompt_files)
    attachments = resolve_prompt_attachments(cfg)
    return identity, prompt_files, prompt_texts, attachments


def _attempt_result(tmp_path: Path, status: str = "success") -> AttemptResult:
    """An AttemptResult shaped like the runner's, over a fake staging dir."""
    run = tmp_path / "staging"
    for sub in ("solutions", "json_logs", "general_logs"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    wb = run / "solutions" / "20260925_120000_1_ApfelInc_Solution_v2_claude_excel_agent_Model.xlsx"
    wb.write_bytes(b"PK\x03\x04 not a real workbook")
    completion = run / "json_logs" / "completion_claude_excel_agent_20260925_115000_ApfelInc.json"
    completion.write_text('{"tasks": [{"task_status": "success"}]}')
    log = run / "general_logs" / "claude_excel_agent_20260925_115000_ApfelInc.log"
    log.write_text("log\n")
    prompts = run / "prompts_ApfelInc_20260925_115000.json"
    prompts.write_text('{"prompts": ["p"], "prompt_version": 205}')
    stamp = house_standards_stamp(attachment_records([HOUSE_STANDARDS]))
    return AttemptResult(
        task_id="1",
        task_name="ApfelInc",
        agent_model_name=LABEL,
        prompt_version=205,
        status=status,
        solution_file=wb,
        log_files=[completion, log],
        started_at="2026-09-25T11:50:00.000001",
        finished_at="2026-09-25T12:00:00.000001",
        duration_seconds=600.0,
        prompt_files=[prompts],
        extra={
            "return_code": 0 if status == "success" else 1,
            "task_metadata": {
                "source_kind": "bundle", "db_task_id": 1, "overrides": {},
                "task_source": "v2",
            },
            "extra_configs": {
                "cdp_port": 9222,
                "infra_tries": 1,
                "engine_task_status": status,
                "house_standards": stamp,
            },
            **({"failure_reason": status} if status != "success" else {}),
        },
    )


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---- (1) TaskSpec parity: bundle vs the cloud source's row-to-spec code -----


@needs_workbooks
def test_bundle_spec_matches_postgres_row_to_spec(tmp_path):
    bundle = _bundle_spec(tmp_path)
    cloud = _fixture_spec(tmp_path)

    assert bundle.task_id == cloud.task_id == "1"
    assert bundle.task_name == cloud.task_name == "ApfelInc"
    assert bundle.solution_name is None and cloud.solution_name is None
    # Same files, same order, same names, same bytes (the bundle is read in
    # place; the cloud path lands in the scratch layout).
    assert [p.name for p in bundle.upload_files] == [p.name for p in cloud.upload_files]
    assert [_sha(p) for p in bundle.upload_files] == [_sha(p) for p in cloud.upload_files]
    assert bundle.upload_files[0] == TASK_1_DIR / "starting_files" / "ApfelInc.xlsx"
    assert cloud.upload_files[0].parent == (
        tmp_path / "scratch" / "excel" / "task_id=1" / "starting_files"
    )
    # Same metadata keys, same values — source_kind names the provenance
    # ("bundle" / "postgres_s3") and never reaches a row (the sink reads only
    # db_task_id and task_source from it).
    assert list(bundle.metadata) == list(cloud.metadata)
    assert bundle.metadata["source_kind"] == "bundle"
    assert cloud.metadata["source_kind"] == "postgres_s3"
    strip = lambda m: {k: v for k, v in m.items() if k != "source_kind"}  # noqa: E731
    assert strip(bundle.metadata) == strip(cloud.metadata) == {
        "db_task_id": 1, "overrides": {}, "task_source": "v2",
    }


@needs_workbooks
def test_bundle_source_filters_and_order(tmp_path):
    src = BundleTaskSource(
        data_root=DATA_ROOT, output_root=tmp_path / "outputs",
        agent_model_name=LABEL, prompt_version=205, skip_already_attempted=False,
    )
    ids = [int(s.task_id) for s in src.iter_tasks()]
    assert ids == sorted(ids) and ids[:3] == [1, 2, 3] and len(ids) == 101
    src = BundleTaskSource(
        data_root=DATA_ROOT, output_root=tmp_path / "outputs",
        agent_model_name=LABEL, prompt_version=205, task_ids=[3, 1],
        task_sources=["v2"], skip_already_attempted=False,
    )
    assert [s.task_id for s in src.iter_tasks()] == ["1", "3"]  # ORDER BY id
    src = BundleTaskSource(
        data_root=DATA_ROOT, output_root=tmp_path / "outputs",
        agent_model_name=LABEL, prompt_version=205, task_ids=[1],
        task_sources=["no_such_source"], skip_already_attempted=False,
    )
    assert list(src.iter_tasks()) == []


# ---- (2) prompt payload for 205 is byte-identical from either spec ---------


@needs_workbooks
def test_prompt_payload_byte_identical(tmp_path):
    cfg = _load_run_config(tmp_path)
    identity, prompt_files, prompt_texts, attachments = _prompt_context(cfg)
    assert cfg.agent.prompt_version == 205
    assert prompt_files == ["tasks_configs/prompts/v2_3.txt"]
    recs = attachment_records(attachments)

    bundle = _bundle_spec(tmp_path)
    cloud = _fixture_spec(tmp_path)
    ec_b = build_engine_config(cfg, bundle, identity, prompt_texts, attachments=attachments)
    ec_c = build_engine_config(cfg, cloud, identity, prompt_texts, attachments=attachments)

    # Everything the engine receives is equal; upload_files differ only in
    # the directory the task's own files are read from (none for task 1).
    drop = {"upload_files"}
    assert {k: v for k, v in ec_b.items() if k not in drop} == {
        k: v for k, v in ec_c.items() if k not in drop
    }
    assert ec_b["prompts"] == ec_c["prompts"] == prompt_texts
    assert ec_b["prompt_version"] == 205
    assert ec_b["template_file"] == "ApfelInc.xlsx"

    started = datetime(2026, 9, 25, 12, 0, 0)
    p_b = _write_prompts_file(tmp_path / "b", bundle.task_name, ec_b, prompt_files, started, recs)
    p_c = _write_prompts_file(tmp_path / "c", cloud.task_name, ec_c, prompt_files, started, recs)
    assert p_b.name == p_c.name == "prompts_ApfelInc_20260925_120000.json"
    assert p_b.read_bytes() == p_c.read_bytes()
    payload = json.loads(p_b.read_text())
    assert payload["prompt_version"] == 205
    assert payload["prompts"] == prompt_texts
    assert [a["name"] for a in payload["attachments"]] == ["House_Standards_v1.md"]
    assert payload["attachments"][0]["sha256"] == _sha(HOUSE_STANDARDS)


# ---- (3) the attachment list is identical ----------------------------------


@needs_workbooks
def test_attachment_list_identical(tmp_path):
    cfg = _load_run_config(tmp_path)
    identity, _files, prompt_texts, attachments = _prompt_context(cfg)
    assert attachments == [HOUSE_STANDARDS.resolve()]

    bundle = _bundle_spec(tmp_path)
    cloud = _fixture_spec(tmp_path)
    ec_b = build_engine_config(cfg, bundle, identity, prompt_texts, attachments=attachments)
    ec_c = build_engine_config(cfg, cloud, identity, prompt_texts, attachments=attachments)

    # upload_files + House Standards, as the panel receives them.
    full_b = [p.name for p in bundle.upload_files] + [a.name for a in attachments]
    full_c = [p.name for p in cloud.upload_files] + [a.name for a in attachments]
    assert full_b == full_c == ["ApfelInc.xlsx", "House_Standards_v1.md"]
    # The panel list (workbook opened from OneDrive by name, so not uploaded).
    assert [Path(p).name for p in ec_b["upload_files"]] == [
        Path(p).name for p in ec_c["upload_files"]
    ] == ["House_Standards_v1.md"]
    assert [_sha(p) for p in ec_b["upload_files"]] == [_sha(p) for p in ec_c["upload_files"]]
    assert preflight_check(ec_b, attachments) == []
    assert preflight_check(ec_c, attachments) == []


# ---- (4) the local sink writes a task_attempts-shaped row ------------------


def test_local_sink_row_has_exact_columns_and_repo_relative_paths(tmp_path, monkeypatch):
    out_root = tmp_path / "outputs"
    monkeypatch.setenv("SPREADSHEETSMITH_OUTPUT_ROOT", str(out_root))
    cfg = _load_run_config(tmp_path)
    sink = build_sink(cfg)
    assert isinstance(sink, LocalAttemptSink)
    assert sink.retains_files is True
    assert sink.output_root == out_root

    result = _attempt_result(tmp_path)
    before_ms = int(time.time() * 1000)
    sink.publish(result)

    rows_path = attempts_file(out_root, LABEL)
    assert rows_path == out_root / LABEL / "task_attempts.jsonl"
    rows = _read_rows(rows_path)
    assert len(rows) == 1
    row = rows[0]

    # Exactly the task_attempts columns, in table order.
    assert list(row) == list(TASK_ATTEMPTS_COLUMNS)

    # Repo-relative POSIX paths under outputs/<label>/task_id=1/<ts>/, each
    # resolving (through the configured root) to a real copy; solution
    # workbook first, then the logs; prompts JSON separately.
    all_paths = row["attempt_files"] + row["prompt_files"]
    assert len(all_paths) == 4
    for rel in all_paths:
        assert not PurePosixPath(rel).is_absolute() and "\\" not in rel
        assert rel.startswith(f"{OUTPUT_PREFIX}/{LABEL}/task_id=1/")
        assert output_path(rel, out_root).is_file(), rel
    assert len({Path(p).parent for p in all_paths}) == 1
    assert [Path(p).name for p in row["attempt_files"]] == [
        result.solution_file.name, *(p.name for p in result.log_files)
    ]
    assert row["attempt_files"][0].endswith(".xlsx")
    assert [Path(p).name for p in row["prompt_files"]] == [result.prompt_files[0].name]
    folder = Path(all_paths[0]).parent.name
    datetime.strptime(folder, "%Y%m%d_%H%M%S")

    # Row values.
    assert isinstance(row["id"], int) and row["id"] >= before_ms
    assert row["task_id"] == 1
    assert row["agent_model_name"] == LABEL
    assert row["agent_model_type"] == "excel"
    assert row["prompt_version"] == 205
    assert row["agent_failed"] is False and row["agent_failed_reason"] is None
    assert row["deprecated"] is False and row["deprecated_reason"] is None
    assert row["cost"] is None and row["context_reduced"] is None
    assert row["updated_at"] is None
    assert row["time_taken_min"] == 10.0
    for key in ("start_time", "end_time", "created_at"):
        assert datetime.fromisoformat(row[key]).tzinfo is not None, key
    assert set(row["extra_configs"]) == EXCEL_EXTRA_CONFIGS_KEYS
    assert row["extra_configs"]["provider"] == "claude_excel_agent"
    assert row["extra_configs"]["ui_model_label"] == "Fable 5.1"
    assert row["extra_configs"]["house_standards"]["file"] == "House_Standards_v1.md"
    assert row["extra_configs"]["engine_task_status"] == "success"

    # The sink copied: the staging originals are untouched.
    assert result.solution_file.is_file()

    # Two rows never share an id or a folder, even inside one second.
    sink.publish(_attempt_result(tmp_path, status="failed"))
    rows = _read_rows(rows_path)
    assert len(rows) == 2 and rows[1]["id"] > rows[0]["id"]
    assert rows[1]["agent_failed"] is True and rows[1]["agent_failed_reason"] == "failed"
    assert Path(rows[1]["attempt_files"][0]).parent != Path(rows[0]["attempt_files"][0]).parent


def test_local_row_is_the_postgres_insert_row(tmp_path):
    """Both sinks build the row through attempt_row: the cloud sink's INSERT
    values are the local row restricted to the INSERT columns."""
    result = _attempt_result(tmp_path)
    identity = resolve_agent_identity(_load_run_config(tmp_path))
    cloud = object.__new__(SpreadsheetSmithPostgresS3AttemptSink)
    cloud.attempt_schema = TASK_ATTEMPTS_SCHEMA
    local = LocalAttemptSink(
        output_root=tmp_path / "outputs", agent_model_name=LABEL,
        agent_model_type="excel", prompt_version=205, extra_configs=identity.settings(),
    )
    for sink in (cloud, local):
        sink.agent_model_name = LABEL
        sink.agent_model_type = "excel"
        sink.prompt_version = 205
        sink.extra_configs = identity.settings()
    files = ["outputs/x/a.xlsx", "outputs/x/b.json"]
    prompts = ["outputs/x/p.json"]
    full = attempt_row(result, local, attempt_files=files, prompt_files=prompts)
    insert = cloud._attempt_values(result, files, prompts)
    assert list(insert) == list(TASK_ATTEMPTS_SCHEMA.columns)
    assert insert == {c: full[c] for c in TASK_ATTEMPTS_SCHEMA.columns}
    assert set(full["extra_configs"]) == EXCEL_EXTRA_CONFIGS_KEYS
    assert _with_offset(full["start_time"]).isoformat().endswith(
        _with_offset(datetime(2026, 9, 25, 11, 50, 0, 1)).isoformat()[-6:]
    )


# ---- skip_already_attempted reads the same jsonl ---------------------------


@needs_workbooks
def test_skip_already_attempted_reads_local_rows(tmp_path):
    out_root = tmp_path / "outputs"
    identity = resolve_agent_identity(_load_run_config(tmp_path))
    sink = LocalAttemptSink(
        output_root=out_root, agent_model_name=LABEL, agent_model_type="excel",
        prompt_version=205, extra_configs=identity.settings(),
    )

    def ids(**kw) -> list[str]:
        base = dict(
            data_root=DATA_ROOT, output_root=out_root, agent_model_name=LABEL,
            prompt_version=205, task_ids=[1, 2], skip_already_attempted=True,
        )
        return [s.task_id for s in BundleTaskSource(**(base | kw)).iter_tasks()]

    assert ids() == ["1", "2"]                       # no rows yet
    sink.publish(_attempt_result(tmp_path, status="failed"))
    assert ids() == ["1", "2"]                       # agent_failed rows don't count
    sink.publish(_attempt_result(tmp_path))
    assert ids() == ["2"]                            # live non-failed row skips task 1
    assert ids(prompt_version=203) == ["1", "2"]     # other prompt_version
    assert ids(agent_model_name="claude_excel_opus_5") == ["1", "2"]  # other label
    assert ids(skip_already_attempted=False) == ["1", "2"]


# ---- (C) selection rule: explicit kind wins, defaults switch offline -------


def test_offline_fallback_switches_only_defaults(tmp_path, monkeypatch):
    absent = tmp_path / "absent.yaml"
    monkeypatch.setattr(
        registry, "_resolve_db_url_with_source", lambda cfg: ("", "unresolved")
    )
    # Defaults (source postgres_s3, sink local), no url -> bundle + local.
    cfg = load_configs(override_path=absent)
    note = apply_offline_fallback(cfg, *explicit_io_kinds(None, None))
    assert note and "bundle" in note and "database.v2_url" in note
    assert (cfg.source.kind, cfg.source.schema) == ("bundle", None)
    assert (cfg.sink.kind, cfg.sink.schema) == ("local", None)

    # Explicit postgres_s3 stands and still fails on the missing url.
    explicit = {
        "agent_model_name": LABEL,
        "source": {"kind": "postgres_s3", "schema": "spreadsheetsmith"},
        "sink": {"kind": "postgres_s3", "schema": "spreadsheetsmith"},
    }
    cfg = load_configs(override_path=absent, run_config_data=explicit)
    assert explicit_io_kinds(None, explicit) == ("postgres_s3", "postgres_s3")
    assert apply_offline_fallback(cfg, *explicit_io_kinds(None, explicit)) is None
    assert cfg.source.kind == "postgres_s3" and cfg.sink.kind == "postgres_s3"
    with pytest.raises(ValueError, match="no database url"):
        build_source(cfg)

    # A url resolves -> nothing changes.
    monkeypatch.setattr(
        registry, "_resolve_db_url_with_source", lambda cfg: ("postgresql://h/db", "t")
    )
    cfg = load_configs(override_path=absent)
    assert apply_offline_fallback(cfg, None, None) is None
    assert cfg.source.kind == "postgres_s3" and cfg.source.schema == "spreadsheetsmith"

    # Both override forms are read; later layers win.
    assert explicit_io_kinds({"source": {"kind": {"value": "bundle"}}}) == ("bundle", None)
    assert explicit_io_kinds({"sink": {"kind": "local"}}, {"sink": {"kind": "postgres_s3"}}) == (
        None, "postgres_s3",
    )


# ---- (D) the offline run configs -------------------------------------------


@pytest.mark.parametrize(
    "name, provider, ui_model_label, thinking_effort",
    [
        ("claude_excel_fable_5_1", "claude_excel_agent", "Fable 5.1", None),
        ("chatgpt_excel_gpt_5_6_sol_xhigh", "chatgpt_excel_agent", "GPT-5.6 Sol", "Extra High"),
        ("claude_excel_opus_5", "claude_excel_agent", "Opus 5", None),
    ],
)
def test_offline_run_configs(tmp_path, monkeypatch, name, provider, ui_model_label, thinking_effort):
    path = OFFLINE_CONFIGS / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text())
    assert raw["agent_model_name"] == name
    for pinned in ("provider", "ui_model_label", "thinking_effort", "agent_folder"):
        assert pinned not in raw  # identity by reference only
    for machine_key in ("browser", "onedrive_base_path", "database", "aws"):
        assert machine_key not in raw

    monkeypatch.setenv("SPREADSHEETSMITH_OUTPUT_ROOT", str(tmp_path / "outputs"))
    cfg = _load_run_config(tmp_path, path)
    assert cfg.benchmark == "v2"
    assert cfg.prompt_version == 205
    assert (cfg.source.kind, cfg.source.schema) == ("bundle", None)
    assert (cfg.sink.kind, cfg.sink.schema) == ("local", None)
    assert list(cfg.source.filters.task_ids) == list(range(1, 102))
    assert cfg.source.filters.skip_already_attempted is True

    identity = resolve_agent_identity(cfg)
    assert (identity.provider, identity.ui_model_label, identity.thinking_effort) == (
        provider, ui_model_label, thinking_effort,
    )
    assert resolve_prompt_files(cfg) == ["tasks_configs/prompts/v2_3.txt"]
    assert resolve_prompt_attachments(cfg) == [HOUSE_STANDARDS.resolve()]
    assert getattr(cfg, provider).max_sec_per_task == 7200

    source = build_source(cfg)
    assert isinstance(source, BundleTaskSource)
    assert source.task_ids == list(range(1, 102))
    assert source.data_root == DATA_ROOT
    sink = build_sink(cfg)
    assert isinstance(sink, LocalAttemptSink)
    assert sink.attempts_path == tmp_path / "outputs" / name / "task_attempts.jsonl"
    assert sink.extra_configs == identity.settings()


# ---- (F) --dry-run: the whole runner path, no browser --------------------


@needs_workbooks
def test_dry_run_task_1_offline(tmp_path, monkeypatch, capsys):
    import infra.run as run

    monkeypatch.setenv("SPREADSHEETSMITH_OUTPUT_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(
        sys, "argv",
        ["infra.run", "--dry-run", "--task-id", "1", "--run-config", str(FABLE_CONFIG)],
    )
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == run.EXIT_OK
    assert "----- prompt turn: tasks_configs/prompts/v2_3.txt" in out
    assert "template workbook (opened from OneDrive by name): ApfelInc.xlsx" in out
    assert "House_Standards_v1.md  sha256=" + _sha(HOUSE_STANDARDS) in out
    assert f"outputs/{LABEL}/task_id=1/<YYYYmmdd_HHMMSS>/" in out
    assert f"outputs/{LABEL}/task_attempts.jsonl" in out
    # Nothing was written anywhere under the output root.
    assert not (tmp_path / "outputs").exists()
