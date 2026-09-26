"""The offline store: bundled inputs under `data/`, run output under `outputs/`.

This is the whole filesystem side of an offline grading. The judge itself is
untouched — what changes is only WHERE an attempt, its task and its golden
solution are read from, and WHERE the grading row and its staged files are
written. Nothing here imports boto3 or psycopg2.

Layout and row shapes are the contract in `<repo>/data/README.md`:

  data/tasks/task_id=<N>/task.json          one `tasks` row, all columns
                       /starting_files/…    the workbook, case PDF, context docs
                       /solution_files/…    the golden solution(s)
  data/results/task_attempts.jsonl          one `task_attempts` row per bundled attempt
  outputs/<label>/task_attempts.jsonl       the same shape, written by an offline run
  outputs/gradings/gradings.jsonl           one `gradings`-shaped row per offline grading
  outputs/gradings/<grading_id>/            that grading's staged files

Every path stored in a bundled row is repo-relative and POSIX-separated
(`data/tasks/task_id=1/starting_files/Book.xlsx`). `resolve_path` joins it onto
the repository root, first swapping the leading `data/` for the configured
`local.data_root` so a bundle unpacked elsewhere still resolves.

Roots, first hit wins:
  1. $SPREADSHEETSMITH_DATA_ROOT / $SPREADSHEETSMITH_OUTPUT_ROOT
  2. config/config.yaml `local.data_root` / `local.output_root`
  3. the benchmark preset's default (`data` / `outputs` for v2)

A benchmark whose preset carries no roots has no bundle: `--benchmark v1`
refuses the local path rather than half-resolving it.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import repo_config
    from .logger import logger
except ImportError:  # imported as a bare module (utils/ on sys.path)
    import repo_config
    from logger import logger

# <repo>/judge/utils/local_store.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_ROOT_ENV = "SPREADSHEETSMITH_DATA_ROOT"
OUTPUT_ROOT_ENV = "SPREADSHEETSMITH_OUTPUT_ROOT"

# The default bundle layout, and the prefix a bundled path is stored under.
DEFAULT_DATA_ROOT = "data"
DEFAULT_OUTPUT_ROOT = "outputs"

TASKS_DIRNAME = "tasks"
RESULTS_DIRNAME = "results"
ATTEMPTS_FILENAME = "task_attempts.jsonl"
GRADINGS_DIRNAME = "gradings"
GRADINGS_FILENAME = "gradings.jsonl"

# Every column of the `gradings` table, in ordinal order, as a local row must
# carry them. Verified against information_schema by
# tests_offline/test_offline_source_sink.py.
GRADING_COLUMNS = (
    "id",
    "task_id",
    "attempt_id",
    "grader_model",
    "grader_prompts",
    "grader_response",
    "accuracy_grade",
    "formula_grade",
    "format_grade",
    "rubric_version",
    "rubric_weight_version",
    "prompt_version",
    "scored_results",
    "time_elapsed_min",
    "cost",
    "raw_files_path",
    "raw_files",
    "errors_encountered",
    "failed",
    "failed_reason",
    "deprecated",
    "deprecated_reason",
    "solution_context_reduced",
    "attempt_context_reduced",
    "context_reduced_details",
    "agentic_mode",
    "judge_version",
    "created_at",
    "updated_at",
    "grader_reasoning",
)


class LocalStoreError(Exception):
    """The offline store cannot serve what was asked of it."""


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------


def _root(env_name: str, config_key: str, preset_default, benchmark: str | None):
    """Resolve one root, or raise when the benchmark has no bundle.

    The environment wins over everything, including the "not bundled" refusal:
    it is the one way to point the loaders at a bundle built elsewhere.
    config/config.yaml `local.*` says where the roots ARE, not which benchmark
    has one, so it cannot unlock a benchmark whose preset carries no root.
    """
    value = (os.environ.get(env_name) or "").strip()
    if not value:
        if preset_default is None:
            raise LocalStoreError(
                f"{benchmark} inputs are not bundled: the {benchmark} preset in "
                f"utils/misc_utils.py has no {config_key}. Only the v2 benchmark "
                f"ships an offline bundle (see data/README.md); grade "
                f"{benchmark} against its database, or set ${env_name}."
            )
        value = repo_config.repo_value("local", config_key) or preset_default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path)


def _preset(benchmark: str | None, key: str):
    """The benchmark preset's default root, or DEFAULT_* when unselected."""
    try:
        from .misc_utils import BENCHMARKS
    except ImportError:  # imported as a bare module (utils/ on sys.path)
        from misc_utils import BENCHMARKS

    if benchmark is None:
        return DEFAULT_DATA_ROOT if key == "data_root" else DEFAULT_OUTPUT_ROOT
    if benchmark not in BENCHMARKS:
        raise LocalStoreError(f"unknown benchmark {benchmark!r}")
    return BENCHMARKS[benchmark].get(key)


def data_root(benchmark: str | None = None) -> Path:
    """Absolute path of the bundled-input root for this benchmark."""
    return _root(DATA_ROOT_ENV, "data_root", _preset(benchmark, "data_root"), benchmark)


def output_root(benchmark: str | None = None) -> Path:
    """Absolute path of the offline-output root for this benchmark."""
    return _root(
        OUTPUT_ROOT_ENV, "output_root", _preset(benchmark, "output_root"), benchmark
    )


def resolve_path(stored: str, benchmark: str | None = None) -> Path:
    """Absolute path for a repo-relative path stored in a bundled row.

    A leading `data/` is replaced with the configured data root, a leading
    `outputs/` with the output root; anything else is joined onto the
    repository root unchanged. An already-absolute path is returned as is.
    """
    p = Path(str(stored))
    if p.is_absolute():
        return p
    parts = p.parts
    if not parts:
        raise LocalStoreError("empty path in a bundled row")
    if parts[0] == DEFAULT_DATA_ROOT:
        return data_root(benchmark).joinpath(*parts[1:])
    if parts[0] == DEFAULT_OUTPUT_ROOT:
        return output_root(benchmark).joinpath(*parts[1:])
    return REPO_ROOT.joinpath(*parts)


def repo_relative(path) -> str:
    """`path` as a repo-relative POSIX string when it is inside the repo."""
    p = Path(path).resolve()
    try:
        return p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def task_dir(task_id: int, benchmark: str | None = None) -> Path:
    return data_root(benchmark) / TASKS_DIRNAME / f"task_id={task_id}"


def load_task(task_id: int, benchmark: str | None = None) -> dict:
    """The bundled `tasks` row for `task_id`."""
    path = task_dir(task_id, benchmark) / "task.json"
    if not path.is_file():
        raise LocalStoreError(
            f"no bundled task {task_id}: {repo_relative(path)} does not exist. "
            f"The task bundle is tracked in git (see data/README.md)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------


def attempt_row_files(benchmark: str | None = None) -> list[Path]:
    """Every JSONL that can hold an attempt row: the bundle, then any run output.

    `outputs/gradings/` is skipped — it holds gradings, never attempts.
    """
    files = []
    bundled = data_root(benchmark) / RESULTS_DIRNAME / ATTEMPTS_FILENAME
    if bundled.is_file():
        files.append(bundled)
    out = output_root(benchmark)
    if out.is_dir():
        gradings_dir = out / GRADINGS_DIRNAME
        for p in sorted(out.rglob(ATTEMPTS_FILENAME)):
            if gradings_dir in p.parents:
                continue
            files.append(p)
    return files


def _iter_rows(path: Path):
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise LocalStoreError(
                    f"{repo_relative(path)}:{lineno} is not valid JSON: {e}"
                ) from e


def attempt_index(benchmark: str | None = None) -> dict:
    """{attempt_id: (row, source path)} across the bundle and every run output.

    An id present in two files is an error, not a silent winner: the two rows
    would grade to two different workbooks under one attempt_id.
    """
    index: dict = {}
    for path in attempt_row_files(benchmark):
        for row in _iter_rows(path):
            attempt_id = row.get("id")
            if attempt_id is None:
                raise LocalStoreError(
                    f"{repo_relative(path)}: an attempt row has no 'id'"
                )
            if attempt_id in index:
                first = repo_relative(index[attempt_id][1])
                raise LocalStoreError(
                    f"attempt id {attempt_id} appears in both {first} and "
                    f"{repo_relative(path)}. Local ids are millisecond epochs and "
                    f"must be unique; remove or renumber one of the rows."
                )
            index[attempt_id] = (row, path)
    return index


def _file_refs(column, benchmark: str | None):
    """A bundled file column as the [{name, path}] shape extract_file_refs reads.

    Paths are resolved to absolute here, so the judge's staging never depends
    on the process's working directory or on --files-base-dir.
    """
    if column is None:
        return None
    if isinstance(column, str):
        column = [column]
    if isinstance(column, dict):
        column = list(column.values())
    refs = []
    for item in column:
        stored = item.get("path", "") if isinstance(item, dict) else str(item)
        if not stored:
            continue
        name = (
            item.get("name")
            if isinstance(item, dict) and item.get("name")
            else Path(stored).name
        )
        refs.append({"name": name, "path": str(resolve_path(stored, benchmark))})
    return refs


def build_attempt(row: dict, task: dict, benchmark: str | None = None) -> dict:
    """The attempt dict grade_single_attempt expects, from two bundled rows.

    Same keys the DB query produces, so setup_task_folder and the whole
    per-attempt pipeline below it are unchanged.
    """
    return {
        "attempt_id": row["id"],
        "task_id": row["task_id"],
        "prompt_files": _file_refs(row.get("prompt_files"), benchmark),
        "attempt_files": _file_refs(row.get("attempt_files"), benchmark),
        "agent_model_name": row.get("agent_model_name"),
        "agent_model_type": row.get("agent_model_type"),
        "prompt_version": row.get("prompt_version"),
        "agent_failed": row.get("agent_failed"),
        "task_name": task.get("task_name"),
        "task_starting_files": _file_refs(task.get("task_starting_files"), benchmark),
        "task_solution_files": _file_refs(task.get("task_solution_files"), benchmark),
    }


def fetch_attempts_by_ids(attempt_ids, benchmark: str | None = None) -> list[dict]:
    """Bundled attempts for these ids, in the order given; unknown ids raise."""
    index = attempt_index(benchmark)
    missing = [i for i in attempt_ids if i not in index]
    if missing:
        sources = [repo_relative(p) for p in attempt_row_files(benchmark)]
        raise LocalStoreError(
            f"no local attempt row for id(s) {missing}. Searched "
            f"{', '.join(sources) or '(no attempt JSONL found)'}."
        )
    out = []
    for attempt_id in attempt_ids:
        row, _ = index[attempt_id]
        if row.get("deprecated"):
            logger.warning(f"  attempt {attempt_id} is marked deprecated in its row")
        out.append(build_attempt(row, load_task(row["task_id"], benchmark), benchmark))
    return out


_EXCEL_EXTS = (".xlsx", ".xlsm", ".xls")


def _workbook_present(row: dict) -> bool:
    """True when the row's judged workbook (the first Excel file listed) exists on disk."""
    for f in row.get("attempt_files") or []:
        if str(f).lower().endswith(_EXCEL_EXTS):
            p = Path(f)
            return (p if p.is_absolute() else REPO_ROOT / p).exists()
    return False


def fetch_all_attempts(benchmark: str | None = None) -> list[dict]:
    """Every non-deprecated attempt whose workbook is on disk, ordered by id.

    The recorded leaderboard rows under data/results/ point at workbooks that
    are not distributed with the repository; --all-local therefore grades what
    is actually present (a reviewer's own runs under outputs/, plus any
    recorded workbooks that have been put in place) and says how many rows it
    passed over.
    """
    index = attempt_index(benchmark)
    rows = [r for r, _ in index.values() if not r.get("deprecated")]
    present = [r for r in rows if _workbook_present(r)]
    skipped = len(rows) - len(present)
    if skipped:
        print(f"--all-local: {skipped} recorded attempt(s) skipped because their workbook is not on disk; "
              f"{len(present)} attempt(s) with a workbook selected")
    present.sort(key=lambda r: r["id"])
    return [build_attempt(r, load_task(r["task_id"], benchmark), benchmark) for r in present]


def fetch_attempts_by_task_ids(task_ids, benchmark: str | None = None) -> list[dict]:
    """Every non-deprecated bundled attempt for these tasks, by (task_id, id)."""
    wanted = set(task_ids)
    index = attempt_index(benchmark)
    rows = [
        r
        for r, _ in index.values()
        if r.get("task_id") in wanted and not r.get("deprecated")
    ]
    rows.sort(key=lambda r: (r["task_id"], r["id"]))
    return [build_attempt(r, load_task(r["task_id"], benchmark), benchmark) for r in rows]


def read_attempt_ids_file(path) -> list[int]:
    """The attempt ids in `path`, in file order, de-duplicated.

    Accepts the three shapes the bundle already uses: a bare JSON list, an
    `{"attempt_ids": [...]}` document (the experiment id files), and a pointer
    manifest whose `rows` carry an `attempt_id` each (leaderboard_ids.json).
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, dict):
        if isinstance(doc.get("rows"), list):
            doc = [
                r["attempt_id"]
                for r in doc["rows"]
                if isinstance(r, dict) and "attempt_id" in r
            ]
        else:
            for key in ("attempt_ids", "ids", "attempts"):
                if key in doc:
                    doc = doc[key]
                    break
    if not isinstance(doc, list) or not all(isinstance(i, int) for i in doc):
        raise LocalStoreError(
            f"{path} must hold a JSON list of integer attempt ids, an "
            f'{{"attempt_ids": [...]}} document, or a pointer manifest whose '
            f'"rows" each carry an "attempt_id".'
        )
    return list(dict.fromkeys(doc))


# ---------------------------------------------------------------------------
# Gradings sink
# ---------------------------------------------------------------------------

_last_local_id = 0


def new_local_id() -> int:
    """A millisecond-epoch id, strictly increasing within this process.

    Two gradings finishing in the same millisecond would otherwise collide and
    overwrite each other's staged folder.
    """
    global _last_local_id
    local_id = max(int(time.time() * 1000), _last_local_id + 1)
    _last_local_id = local_id
    return local_id


def gradings_dir(benchmark: str | None = None) -> Path:
    return output_root(benchmark) / GRADINGS_DIRNAME


def stage_grading_files(output_dir, grading_id: int, benchmark: str | None = None):
    """Copy a grading's staged files into `outputs/gradings/<id>/`.

    Returns (repo-relative folder, sorted relative POSIX paths) — the same pair
    the S3 sink returns for raw_files_path / raw_files.
    """
    dest = gradings_dir(benchmark) / str(grading_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise LocalStoreError(f"{repo_relative(dest)} already exists")
    shutil.copytree(str(output_dir), str(dest))
    raw_files = sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()
    )
    return repo_relative(dest), raw_files


def append_grading(row: dict, benchmark: str | None = None) -> Path:
    """Append one gradings-shaped row to `outputs/gradings/gradings.jsonl`."""
    unknown = sorted(set(row) - set(GRADING_COLUMNS))
    missing = sorted(set(GRADING_COLUMNS) - set(row))
    if unknown or missing:
        raise LocalStoreError(
            f"local grading row does not match the gradings columns "
            f"(unknown: {unknown}, missing: {missing})"
        )
    path = gradings_dir(benchmark) / GRADINGS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({c: row[c] for c in GRADING_COLUMNS}, default=str) + "\n")
    return path


def now_iso() -> str:
    """Timestamp in the bundle's format: ISO-8601 with an offset."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Judge-reliability toys
# ---------------------------------------------------------------------------

RELIABILITY_DIRNAME = "judge_reliability"
TOY_TASKS_FILENAME = "toy_tasks.jsonl"
TOY_RUNS_FILENAME = "toy_runs.jsonl"
TOY_GRADINGS_FILENAME = "toy_gradings.jsonl"
TOY_FILES_DIRNAME = "toy_tasks"
TOY_ARCHIVE_NAME = "the judge-reliability toy archive (data/judge_reliability/toy_tasks/)"


def reliability_data_dir(benchmark: str | None = None) -> Path:
    return data_root(benchmark) / RELIABILITY_DIRNAME


def reliability_output_dir(benchmark: str | None = None) -> Path:
    return output_root(benchmark) / RELIABILITY_DIRNAME


def load_toy_tasks(benchmark: str | None = None) -> list[dict]:
    """The bundled `judge_reliability.toy_tasks` rows, by check number.

    Applies the same filter the database query does: not deprecated, and not
    excluded by the alignment review.
    """
    path = reliability_data_dir(benchmark) / TOY_TASKS_FILENAME
    if not path.is_file():
        raise LocalStoreError(
            f"no bundled toy tasks: {repo_relative(path)} does not exist "
            f"(see data/README.md)."
        )
    rows = [
        r
        for r in _iter_rows(path)
        if not r.get("deprecated") and (r.get("alignment_verdict") or "") != "excluded"
    ]
    rows.sort(key=lambda r: r["check_no"])
    return rows


def toy_workbook(
    toy: dict, variant: str, benchmark: str | None = None, must_exist: bool = False
) -> Path:
    """Where the toy's Pass or Fail workbook is, present or not.

    The toy corpus is about a gigabyte, so it ships as a sidecar rather than in
    git: the rows carry its paths and hashes, the files arrive separately. The
    path resolves either way — a plan, a listing or a dry run is still useful
    without the archive — and `must_exist` is what turns a missing file into an
    error, at the point where one is actually about to be read.
    """
    path = resolve_path(toy[f"s3_{variant}"], benchmark)
    if must_exist and not path.is_file():
        raise LocalStoreError(missing_toy_message(path))
    return path


def missing_toy_message(path) -> str:
    return (
        f"toy workbook missing: {repo_relative(path)}. The toy rows are tracked "
        f"but their workbooks are not — unpack {TOY_ARCHIVE_NAME} into place "
        f"first; scripts/verify_offline_bundle.py checks it."
    )


def append_reliability_row(filename: str, row: dict, benchmark: str | None = None) -> Path:
    """Append one row to outputs/judge_reliability/<filename>."""
    path = reliability_output_dir(benchmark) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return path


def read_reliability_rows(filename: str, benchmark: str | None = None) -> list[dict]:
    """Rows from a reliability JSONL: the bundled one, then any this box wrote."""
    rows = []
    for path in (
        reliability_data_dir(benchmark) / filename,
        reliability_output_dir(benchmark) / filename,
    ):
        if path.is_file():
            rows.extend(_iter_rows(path))
    return rows


def stage_toy_grading_files(
    output_dir, run_id: str, grading_id: int, benchmark: str | None = None
):
    """Copy a toy grading's staged files under outputs/judge_reliability/<run_id>/.

    Returns (repo-relative folder, sorted relative POSIX paths), the same pair
    the object-store sink returns.
    """
    dest = reliability_output_dir(benchmark) / run_id / str(grading_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise LocalStoreError(f"{repo_relative(dest)} already exists")
    shutil.copytree(str(output_dir), str(dest))
    raw_files = sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()
    )
    return repo_relative(dest), raw_files
