"""Path conventions of the offline bundle (`data/`) and offline runs (`outputs/`).

See <repo>/data/README.md. Every path written into a bundle or output JSON
is repo-relative and POSIX-separated, with the canonical prefixes `data/`
and `outputs/`. When `local.data_root` / `local.output_root` (or the env
overrides SPREADSHEETSMITH_DATA_ROOT / SPREADSHEETSMITH_OUTPUT_ROOT) point somewhere else,
loaders swap the canonical prefix for that root — the recorded paths never
change, so a bundle or an outputs tree can be moved without rewriting a
row.

Pure path arithmetic; nothing here reads config or touches the filesystem
beyond `Path` operations.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

# The monorepo root: <repo>/excel-agents-master/task_io/local_layout.py.
MEMBER_ROOT = Path(__file__).resolve().parents[1]
MONOREPO_ROOT = MEMBER_ROOT.parent

DATA_PREFIX = "data"
OUTPUT_PREFIX = "outputs"
DEFAULT_DATA_ROOT = "data"
DEFAULT_OUTPUT_ROOT = "outputs"

DATA_ROOT_ENV = "SPREADSHEETSMITH_DATA_ROOT"
OUTPUT_ROOT_ENV = "SPREADSHEETSMITH_OUTPUT_ROOT"

TASKS_DIR = "tasks"
TASK_DIR_PREFIX = "task_id="
TASK_ROW_FILE = "task.json"
ATTEMPTS_FILE = "task_attempts.jsonl"


def resolve_root(value: str | Path | None, default: str) -> Path:
    """An absolute root from a config value: absolute paths stand, relative
    ones resolve against the monorepo root."""
    raw = Path(value).expanduser() if value else Path(default)
    return raw if raw.is_absolute() else (MONOREPO_ROOT / raw).resolve()


def canonical_path(root: Path, prefix: str, path: Path) -> str:
    """The repo-relative POSIX form recorded in JSON for a file under
    `root`: `<prefix>/<path relative to root>`."""
    rel = Path(path).resolve().relative_to(Path(root).resolve())
    return str(PurePosixPath(prefix) / PurePosixPath(*rel.parts))


def resolve_path(rel: str, root: Path, prefix: str) -> Path:
    """The on-disk location of a recorded repo-relative path: the canonical
    `<prefix>/` head is swapped for `root`; anything else resolves against
    the monorepo root (absolute paths stand)."""
    p = PurePosixPath(rel)
    if p.is_absolute():
        return Path(rel)
    parts = p.parts
    if parts and parts[0] == prefix:
        return Path(root).joinpath(*parts[1:])
    return MONOREPO_ROOT.joinpath(*parts)


def data_path(rel: str, data_root: Path) -> Path:
    return resolve_path(rel, data_root, DATA_PREFIX)


def output_path(rel: str, output_root: Path) -> Path:
    return resolve_path(rel, output_root, OUTPUT_PREFIX)


def attempts_file(output_root: Path, agent_model_name: str) -> Path:
    """outputs/<agent_model_name>/task_attempts.jsonl (a label with a `/`
    nests one level, verbatim)."""
    return Path(output_root).joinpath(*agent_model_name.split("/")) / ATTEMPTS_FILE
