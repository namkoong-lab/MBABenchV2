"""The offline path must produce the same experiment as the database path.

Four things have to hold for a run out of `data/` to be the same experiment
as the recorded wave, and each is one test below:

1. the task object the runner receives is the database row, with only the
   two file columns rewritten to the bundled copies;
2. the model's input — system prompt and assembled user message — is
   byte-identical between a workspace staged from the bundle and one staged
   by download;
3. the attachment set the workspace ends up holding is identical;
4. a row the local sink appends carries exactly the `task_attempts` column
   set, no more and no less.

Everything here is offline: no database, no object store, no API and no MCP
server. The two database facts the assertions lean on — the tasks rows and
the task_attempts column list — are pinned in tests/fixtures/ (read-only
SELECTs, provenance in each file's `_source`), so this test keeps checking
the real schema on a machine that has never had credentials.

Run:  pytest tests/test_offline_parity.py
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from excel_cli_agent import repo_config
from excel_cli_agent.auto_batch_runner import AutoBatchRunner, TaskInfo
from excel_cli_agent.local_sink import ATTEMPT_COLUMNS, LocalAttemptSink
from excel_cli_agent.local_source import LocalTaskSource
from excel_cli_agent.prompt_versions import (
    PROMPT_VERSIONS, PROMPTS_DIR, attachment_names_for, attachments_for,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The cohort the CLI leaderboard ran under; its prompt set is what parity is
# asserted for.
PROMPT_VERSION = "v16"
BENCHMARK = AutoBatchRunner.BENCHMARKS["v2"]
# The task workbooks are not tracked; scripts/install_task_files.py unpacks them
# from the downloaded zip. Until then the bundle cannot be opened at all.
NO_WORKBOOKS = "task workbooks not installed — see README 'Task files' (scripts/install_task_files.py)"
TASK1_WORKBOOK = (repo_config.data_root(BENCHMARK["data_root"])
                  / "tasks" / "task_id=1" / "starting_files" / "ApfelInc.xlsx")
needs_workbooks = pytest.mark.skipif(not TASK1_WORKBOOK.is_file(), reason=NO_WORKBOOKS)


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def db_rows():
    """The `tasks` rows as the database holds them, s3:// URIs and all."""
    return {row["id"]: row for row in _fixture("db_tasks_rows.json")["rows"]}


@pytest.fixture(scope="module")
def bundle():
    data_root = repo_config.data_root(BENCHMARK["data_root"])
    if not (data_root / "tasks").is_dir():
        pytest.skip(f"no task bundle at {data_root} (see data/README.md)")
    if not TASK1_WORKBOOK.is_file():
        pytest.skip(NO_WORKBOOKS)
    return LocalTaskSource(data_root, benchmark="v2", bundled=True)


# --- 1. the same task object ----------------------------------------------


def test_the_bundle_carries_the_database_row_verbatim(bundle, db_rows):
    """Every column matches, and only the file columns were rewritten."""
    for task_id, db_row in db_rows.items():
        task = bundle.get(task_id)
        assert task is not None, f"task {task_id} missing from the bundle"
        bundled = task.row

        file_columns = {"task_starting_files", "task_solution_files"}
        for column, expected in db_row.items():
            if column in file_columns:
                continue
            assert bundled.get(column) == expected, (
                f"task {task_id}: column {column} differs between the "
                f"database row and the bundled one"
            )

        # The rewritten columns keep the originals under s3_keys, so the
        # bundled row still says which object each copy came from.
        for column in file_columns:
            original = [uri.split("/", 3)[-1] for uri in db_row[column]]
            assert bundled["s3_keys"][column] == original, (
                f"task {task_id}: {column} s3_keys lost the original keys"
            )
            assert len(bundled[column]) == len(db_row[column])


def test_the_task_object_the_runner_gets_points_at_real_files(bundle, db_rows):
    for task_id in db_rows:
        task = bundle.get(task_id)
        assert task.task_starting_files, "a task with no starting files would run empty"
        for path in task.task_starting_files:
            assert Path(path).is_file() and Path(path).stat().st_size > 0
            # The workspace is keyed on the file's name, so a rewritten path
            # that renamed the workbook would change what the agent opens.
            assert Path(path).name == task.row["s3_keys"]["task_starting_files"][
                task.task_starting_files.index(path)
            ].split("/")[-1]


def test_the_bundle_holds_every_task_the_database_does(bundle):
    """101 live `v2` tasks, ids 1..101, in id order — the selection any
    lane config's range is cut from."""
    tasks = bundle.load_all()
    assert [t.id for t in tasks] == sorted(t.id for t in tasks)
    assert {t.task_source for t in tasks} == {"v2"}
    assert not [t for t in tasks if t.deprecated]
    assert len(tasks) == 101


# --- the Gemini cohort's system prompt -------------------------------------

# The cohort that does not run the prompt set's own system prompt. Gemini
# 3.8 Flash answers through Forge with native function calls, so its v16
# system prompt is a variant whose response contract asks for those instead
# of the JSON actions batch; the set's own file would hand it a contract its
# tools contradict. The fixture is the exact artifact recorded on that
# cohort's task-1 attempt (prompt_files), so this asserts against what the
# model was actually sent, not against a description of it.
GEMINI_LABEL = "openpyxl_tensorblock/gemini-3.8-flash-high"
RECORDED_GEMINI_PROMPT = FIXTURES / "recorded_system_prompt_v16_gemini-3.8-flash.txt"


def _gemini_system_prompt_path():
    """The system prompt file a run would send for the Gemini identity.

    Resolved through the runner's own selection call rather than by name, so
    this fails if the mapping changes. Returns None when the model-variant
    selection is not present in this checkout — the variant and the code that
    picks it live on the gemini-api branch, and until that is folded in this
    package has one system prompt per version for every model.
    """
    from excel_cli_agent import prompt_versions
    from excel_cli_agent.agent_identity import resolve_agent_identity

    select = getattr(prompt_versions, "system_prompt_file", None)
    if select is None:
        return None
    try:
        from excel_cli_agent.models_config import uses_gemini_tool_calls
    except ImportError:
        return None

    identity = resolve_agent_identity({"agent_model_name": GEMINI_LABEL})
    model = identity.settings()["model"]
    return PROMPTS_DIR / select(PROMPT_VERSION, model,
                                require_variant=uses_gemini_tool_calls(model))


def test_the_gemini_cohort_sends_the_recorded_system_prompt():
    """Byte equality with the artifact that cohort's attempt recorded."""
    path = _gemini_system_prompt_path()
    if path is None:
        pytest.skip(
            "no model-variant system-prompt selection in this checkout "
            "(prompt_versions.system_prompt_file); the Gemini variant and its "
            "selection are on the gemini-api branch — see "
            "test_the_plain_v16_prompt_is_not_what_the_gemini_cohort_ran"
        )
    assert path.read_bytes() == RECORDED_GEMINI_PROMPT.read_bytes(), (
        f"{path.name} is not byte-identical to the prompt the Gemini cohort "
        "was sent; that cohort's rows would not be reproducible"
    )


def test_the_plain_v16_prompt_is_not_what_the_gemini_cohort_ran():
    """The reason the test above cannot be skipped quietly.

    Without the variant, the Gemini identity falls back to the set's own
    system prompt — and that is a different prompt from the one its recorded
    attempts were run with. Pinned so a skip above can never be mistaken for
    parity: the day the plain file does equal the fixture, this fails and
    says the situation changed.
    """
    plain = PROMPTS_DIR / PROMPT_VERSIONS[PROMPT_VERSION]["system"]
    assert plain.read_bytes() != RECORDED_GEMINI_PROMPT.read_bytes(), (
        "the prompt set's own system prompt now equals the Gemini variant — "
        "re-check which file that cohort should be sent"
    )


# --- 2/3. the same workspace, therefore the same model input ---------------


class _FakeS3:
    """Serves the bundled bytes the way the object store would."""

    def __init__(self, by_key):
        self._by_key = by_key

    def download_file(self, bucket, key, path):
        shutil.copyfile(self._by_key[key], path)


def _runner(tmp_path, attachments, attachment_names):
    runner = object.__new__(AutoBatchRunner)
    runner.config = {"workspace_base_dir": str(tmp_path)}
    runner._attachments = attachments
    runner._attachment_names = attachment_names
    return runner


def _stage_both_ways(tmp_path, bundle, db_row):
    """Build the same task's workspace from the bundle and from 'S3'."""
    task = bundle.get(db_row["id"])
    attachments = repo_config.resolve_attachments(attachments_for(PROMPT_VERSION))
    names = attachment_names_for(PROMPT_VERSION)

    local_runner = _runner(tmp_path / "local", attachments, names)
    local_runner._source_local = True
    local_ws = Path(local_runner.setup_workspace(TaskInfo(
        task_id=task.id, task_name=task.task_name, task_source=task.task_source,
        task_starting_files=list(task.task_starting_files),
    )))

    keys = [uri.split("/", 3)[-1] for uri in db_row["task_starting_files"]]
    cloud_runner = _runner(tmp_path / "cloud", attachments, names)
    cloud_runner._s3_client = _FakeS3(dict(zip(keys, task.task_starting_files)))
    cloud_ws = Path(cloud_runner.setup_workspace(TaskInfo(
        task_id=task.id, task_name=task.task_name, task_source=task.task_source,
        task_starting_files=list(db_row["task_starting_files"]),
    )))
    return local_runner, local_ws, cloud_runner, cloud_ws


def test_both_paths_stage_the_same_workspace(tmp_path, bundle, db_rows):
    """Same names, same bytes — including the house-standards attachment."""
    for db_row in db_rows.values():
        _, local_ws, _, cloud_ws = _stage_both_ways(
            tmp_path / f"t{db_row['id']}", bundle, db_row)

        def contents(ws):
            return {p.name: p.read_bytes() for p in sorted(ws.iterdir()) if p.is_file()}

        local_files, cloud_files = contents(local_ws), contents(cloud_ws)
        assert set(local_files) == set(cloud_files)
        for name in local_files:
            assert local_files[name] == cloud_files[name], f"{name} differs"
        assert "HOUSE_STANDARDS.md" in local_files, (
            "v16 promises HOUSE_STANDARDS.md in the workspace"
        )


def test_both_paths_detect_the_same_context_files(tmp_path, bundle, db_rows):
    """The attachment list handed to the executor — what the agent is told
    it has — must not depend on where the files came from."""
    for db_row in db_rows.values():
        local_runner, local_ws, cloud_runner, cloud_ws = _stage_both_ways(
            tmp_path / f"t{db_row['id']}", bundle, db_row)
        local_cfg = local_runner.detect_workspace_files(str(local_ws))
        cloud_cfg = cloud_runner.detect_workspace_files(str(cloud_ws))
        assert local_cfg.detected_excel_files == cloud_cfg.detected_excel_files
        assert local_cfg.detected_pdf_files == cloud_cfg.detected_pdf_files
        assert local_cfg.detected_text_files == cloud_cfg.detected_text_files


def _assembled_message(runner, workspace, workspace_config):
    """The user message the model would receive for this workspace.

    Built with the executor's own code, one step short of the API call, so
    the comparison is of the real prompt rather than of a reconstruction.
    The executor needs nothing from the MCP server at this point — only the
    storage path — so no server is started.
    """
    from excel_cli_agent.task_executor import (
        ExcelTaskExecutor, TaskExecution, TaskStatus,
    )

    system_prompt_path = PROMPTS_DIR / PROMPT_VERSIONS[PROMPT_VERSION]["system"]
    executor = ExcelTaskExecutor(
        excel_client=type("C", (), {"storage_path": str(workspace)})(),
        api_key="not-used",
        model="claude-fable-5-1",
        fresh_context_mode=True,
        enhanced_excel_context=True,
        recent_history_count=3,
        system_prompt_path=str(system_prompt_path),
    )
    executor.add_context_texts(workspace_config.detected_text_files)
    executor.add_context_pdfs(workspace_config.detected_pdf_files)
    executor.add_context_excels(workspace_config.detected_excel_files)

    template_key = "wsp" if workspace_config.path.endswith("wsp") else "fmwc"
    template = (PROMPTS_DIR / PROMPT_VERSIONS[PROMPT_VERSION][template_key]).read_text(
        encoding="utf-8")
    # The state execute_task starts a task in, with a fixed start_time: a
    # wall-clock value would be the one thing that could differ between the
    # two assemblies for a reason that has nothing to do with the source.
    task = TaskExecution(
        task_id="parity",
        user_prompt=template,
        status=TaskStatus.IN_PROGRESS,
        steps=[],
        start_time=0.0,
        max_iterations=40,
    )
    system_prompt = executor._get_system_prompt()
    return system_prompt, executor._assemble_context(task, system_prompt)


def test_the_model_input_is_byte_identical(tmp_path, bundle, db_rows):
    for db_row in db_rows.values():
        local_runner, local_ws, cloud_runner, cloud_ws = _stage_both_ways(
            tmp_path / f"t{db_row['id']}", bundle, db_row)
        local_system, local_user = _assembled_message(
            local_runner, local_ws, local_runner.detect_workspace_files(str(local_ws)))
        cloud_system, cloud_user = _assembled_message(
            cloud_runner, cloud_ws, cloud_runner.detect_workspace_files(str(cloud_ws)))

        assert local_system == cloud_system
        # The workspace path appears in neither message; if it ever did, this
        # is where the two would diverge, and that is worth knowing.
        assert local_user == cloud_user, (
            f"task {db_row['id']}: the assembled user message differs between "
            "the bundled and the downloaded workspace"
        )
        assert str(local_ws) not in local_user


def test_the_selected_task_template_follows_task_source_either_way(bundle, db_rows):
    runner = object.__new__(AutoBatchRunner)
    for db_row in db_rows.values():
        task = bundle.get(db_row["id"])
        assert task.task_source == db_row["task_source"]
        assert runner.select_task_template(task.task_source) == \
            runner.select_task_template(db_row["task_source"])


# --- 4. the recorded row ---------------------------------------------------


def test_a_local_row_carries_exactly_the_database_columns(tmp_path):
    expected = _fixture("db_task_attempts_columns.json")["columns"]
    assert list(ATTEMPT_COLUMNS) == expected, (
        "local_sink.ATTEMPT_COLUMNS has drifted from the task_attempts table"
    )

    sink = LocalAttemptSink(tmp_path, "openpyxl_anthropic/claude-fable-5-1-max")
    written = sink.append_row({
        "id": sink.new_attempt_id(),
        "task_id": 1,
        "agent_model_name": "openpyxl_anthropic/claude-fable-5-1-max",
        "agent_model_type": "api",
        "attempt_files": ["outputs/x/task_id=1/20260925_120000/solution.xlsx"],
        "prompt_files": ["outputs/x/prompts/20260925_120000_system_prompt_v16.txt"],
        "start_time": "2026-09-25T12:00:00+02:00",
        "end_time": "2026-09-25T12:30:00+02:00",
        "time_taken_min": 30.0,
        "cost": 1.25,
        "prompt_version": 1609,
        "agent_failed": False,
        "deprecated": False,
        "created_at": "2026-09-25T12:30:01+02:00",
        "extra_configs": {"model": "claude-fable-5-1"},
    })
    assert list(written) == expected
    on_disk = json.loads(sink.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert list(on_disk) == expected
    # Columns the caller left out are present and null, as they are on a
    # database row that never set them.
    assert on_disk["agent_failed_reason"] is None
    assert on_disk["context_reduced"] is None


def test_a_row_with_an_unknown_column_is_refused(tmp_path):
    sink = LocalAttemptSink(tmp_path, "cohort")
    with pytest.raises(ValueError, match="not task_attempts columns"):
        sink.append_row({"task_id": 1, "grade": 99})


def test_the_cohort_label_nests_and_resume_reads_what_was_written(tmp_path):
    """A label with a slash nests one level, and the JSONL the sink appends
    to is the one the skip logic reads back."""
    sink = LocalAttemptSink(tmp_path, "openpyxl_tensorblock/kimi-k3-max")
    assert sink.cohort_dir == tmp_path / "openpyxl_tensorblock" / "kimi-k3-max"

    assert sink.trial_count(7) == 0 and not sink.has_success(7)
    sink.append_row({"id": 1, "task_id": 7, "agent_failed": True,
                     "created_at": "2026-09-25T00:00:00+00:00"})
    assert sink.trial_count(7) == 1 and not sink.has_success(7)
    sink.append_row({"id": 2, "task_id": 7, "agent_failed": False,
                     "created_at": "2026-09-25T00:00:01+00:00"})
    assert sink.trial_count(7) == 2 and sink.has_success(7)
    # trials_since excludes older rows, the way the database filter does.
    assert sink.trial_count(7, since="2026-09-26") == 0
    # A deprecated row counts for neither.
    sink.append_row({"id": 3, "task_id": 7, "agent_failed": False, "deprecated": True,
                     "created_at": "2026-09-25T00:00:02+00:00"})
    assert sink.trial_count(7) == 2

    # A half-written final line (a lane killed mid-append) must not stop the
    # relaunch that is meant to recover it.
    with open(sink.jsonl_path, "a", encoding="utf-8") as fh:
        fh.write('{"id": 4, "task_i')
    assert sink.trial_count(7) == 2


# --- the storage selection rule -------------------------------------------


@needs_workbooks
def test_a_configured_database_is_never_silently_bypassed(monkeypatch):
    """Saying nothing with a URL configured keeps the cloud path; saying
    nothing without one goes local. Both directions matter: the first is
    what stops a wave landing in outputs/ unnoticed."""
    runner = object.__new__(AutoBatchRunner)
    runner.config = {}
    config = {"agent_model_name": "openpyxl_anthropic/claude-fable-5-1-max"}

    import excel_cli_agent.auto_batch_runner as abr
    monkeypatch.setattr(abr, "resolve_db_url",
                        lambda b: ("postgresql://u@h/SpreadsheetSmith", "test"))
    runner._select_storage(dict(config), "v2", BENCHMARK)
    assert not runner._source_local and not runner._sink_local

    monkeypatch.setattr(abr, "resolve_db_url", lambda b: ("", "unresolved"))
    runner = object.__new__(AutoBatchRunner)
    runner._select_storage(dict(config), "v2", BENCHMARK)
    assert runner._source_local and runner._sink_local


@needs_workbooks
def test_an_explicit_local_source_wins_over_a_configured_database(monkeypatch):
    import excel_cli_agent.auto_batch_runner as abr
    monkeypatch.setattr(abr, "resolve_db_url",
                        lambda b: ("postgresql://u@h/SpreadsheetSmith", "test"))
    runner = object.__new__(AutoBatchRunner)
    runner._select_storage(
        {"agent_model_name": "openpyxl_anthropic/claude-fable-5-1-max",
         "source": "local", "sink": "local"}, "v2", BENCHMARK)
    assert runner._source_local and runner._sink_local


def test_asking_for_the_database_without_a_url_is_an_error(monkeypatch):
    import excel_cli_agent.auto_batch_runner as abr
    monkeypatch.setattr(abr, "resolve_db_url", lambda b: ("", "unresolved"))
    runner = object.__new__(AutoBatchRunner)
    with pytest.raises(ValueError, match="No database URL"):
        runner._select_storage(
            {"agent_model_name": "openpyxl_anthropic/claude-fable-5-1-max",
             "sink": "postgres"}, "v2", BENCHMARK)


def test_v1_inputs_are_not_bundled(monkeypatch):
    import excel_cli_agent.auto_batch_runner as abr
    monkeypatch.setattr(abr, "resolve_db_url", lambda b: ("", "unresolved"))
    runner = object.__new__(AutoBatchRunner)
    with pytest.raises(Exception, match="not bundled"):
        runner._select_storage(
            {"agent_model_name": "openpyxl_anthropic/claude-fable-5-1-max"},
            "v1", AutoBatchRunner.BENCHMARKS["v1"])


def test_no_task_in_this_benchmark_has_more_than_one_starting_file():
    """Pinned so the single-file assumption in the parity fixtures is a
    recorded fact about the task set, not an accident of which two rows
    happened to be sampled."""
    assert _fixture("db_tasks_rows.json")["max_starting_files_across_all_tasks"] == 1
