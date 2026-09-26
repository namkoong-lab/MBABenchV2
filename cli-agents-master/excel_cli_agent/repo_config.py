"""Read the monorepo config at <SpreadsheetSmith>/config/config.yaml.

cli-agents-master is a workspace member of SpreadsheetSmith, whose root
pyproject.toml exposes config/python/config.py as the top-level `config`
module. That file is the single home for both benchmark database URLs
(database.v1_url / database.v2_url), the AWS credentials, and the S3 bucket
name — so a batch config's `benchmark:` key selects the right database on
its own, with no DATABASE_URL swapping in .env.

RESOLUTION ORDER (database url and AWS keys alike), first hit wins:

  1. the monorepo config — the only benchmark-aware layer.
  2. the environment (DATABASE_URL / boto3's default chain). This is the
     standalone-checkout path, where the `config` module does
     not exist and the env var is all there is.

Model API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) stay in .env — the
monorepo config holds no keys for them.

OFFLINE ROOTS. A run with no database and no object store reads its tasks
from the bundle in <repo>/data and writes its attempts under <repo>/outputs
(see data/README.md for both layouts). Those two locations resolve in the
same first-hit-wins order as everything else:

  1. the environment (SPREADSHEETSMITH_DATA_ROOT / SPREADSHEETSMITH_OUTPUT_ROOT) — a
     one-run override, e.g. a bundle unpacked on an external disk.
  2. the monorepo config (local.data_root / local.output_root).
  3. the benchmark preset's default ("data" / "outputs").

A relative value is resolved against the monorepo root, so the same config
works from any cwd.
"""

import hashlib
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Points the monorepo-config lookup at a different directory. Used by the
# tests; also an escape hatch for layouts where the installed location is
# wrong. Unset -> Config resolves its own directory via the editable install.
REPO_CONFIG_DIR_ENV = "SPREADSHEETSMITH_CONFIG_DIR"

# Per-run overrides for the two offline roots (see the module docstring).
# Highest precedence: a reviewer who unpacked the bundle elsewhere should not
# have to edit a config file to run from it.
DATA_ROOT_ENV = "SPREADSHEETSMITH_DATA_ROOT"
OUTPUT_ROOT_ENV = "SPREADSHEETSMITH_OUTPUT_ROOT"

# The root the paths inside the bundle's JSON are written against
# (data/tasks/...). A different data_root replaces this leading segment.
BUNDLE_PATH_PREFIX = "data"


def repo_value(*path: str) -> Optional[str]:
    """Non-empty string at a path in <repo>/config/config.yaml, else None.

    `from config import Config` works because the root pyproject.toml exposes
    config/python/config.py as a top-level module, so an editable install
    resolves it from any cwd. This wrapper adds the three things that import
    does not give you:

    * It never raises. A standalone checkout has no config/
      directory, so the import fails there — and that failure IS the signal
      to fall through to the environment, not an error.
    * It never writes. Config.load() defaults to create_missing=True, which
      seeds a config.yaml from the defaults as a side effect of reading one.
    * A `null` placeholder (config_default.yaml ships `access_key_id: null`)
      yields None, so an unset key falls through to the next layer rather
      than resolving to something falsy-but-present.
    """
    try:
        from config import Config
    except ImportError:
        logger.debug("monorepo `config` module not installed; using env vars")
        return None

    override = os.environ.get(REPO_CONFIG_DIR_ENV)
    # Config.load warns about every unset ${env:VAR} in the file, including
    # keys cli-agents never reads (gemini_api_key, ...). Not worth a warning
    # to someone starting a batch run.
    cfg_log = logging.getLogger("config")
    prev_level = cfg_log.level
    cfg_log.setLevel(logging.ERROR)
    try:
        data = Config.load(
            Path(override).expanduser() if override else None,
            create_missing=False,
            check_required=False,
        ).as_dict()
    except Exception as e:  # degrade, never break the caller
        logger.warning(
            "could not read the monorepo config (%s: %s); falling back to "
            "environment variables",
            type(e).__name__,
            e,
        )
        return None
    finally:
        cfg_log.setLevel(prev_level)

    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node.strip() if isinstance(node, str) and node.strip() else None


def resolve_db_url(benchmark: Optional[str]) -> Tuple[str, str]:
    """(url, human-readable provenance). See the module docstring for order.

    `benchmark` is "v1" / "v2"; None skips the (benchmark-keyed) monorepo
    layer and resolves from DATABASE_URL alone.
    """
    if benchmark:
        url = repo_value("database", f"{benchmark}_url")
        if url:
            return url, f"config/config.yaml database.{benchmark}_url"

    env_url = os.environ.get("DATABASE_URL", "") or ""
    if env_url:
        return env_url, "$DATABASE_URL"

    return "", "unresolved"


def _database_name(url: str) -> str:
    """Database name out of a connection string, dropping every credential."""
    tail = url.split("://", 1)[-1].split("/", 1)
    if len(tail) < 2:
        return "?"
    return tail[1].split("?", 1)[0] or "?"


def describe_database_target(benchmark: Optional[str]) -> str:
    """One safe log line naming the DB a run will read/write, and why.

    Never includes the password — batch logs are routinely pasted into
    issues and chats.
    """
    url, source = resolve_db_url(benchmark)
    if not url:
        return f"unresolved ({source})"
    return f"{_database_name(url)} (from {source})"


def boto3_credentials() -> dict:
    """kwargs for boto3.client from config/config.yaml aws.*, or {}.

    {} means "use boto3's default chain" (env vars, AWS_PROFILE, ~/.aws) —
    both keys must come from the config or neither does, so a half-filled
    config can't mix its access key with the profile's secret.
    """
    access_key = repo_value("aws", "access_key_id")
    secret_key = repo_value("aws", "secret_access_key")
    if access_key and secret_key:
        return {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }
    return {}


def monorepo_root() -> Path:
    """<SpreadsheetSmith>, the directory holding config/ and house_standards/.

    Asks the installed `config` module for its directory (config/python/
    config.py -> <root>/config), the same install that makes the database
    and AWS lookups work, so the root can't disagree with them. A standalone
    checkout has no `config` module; there the workspace layout (this file
    lives at <root>/cli-agents-master/excel_cli_agent/) is the only answer.
    """
    try:
        from config import Config
        return Path(Config.DEFAULT_CONFIG_DIR).resolve().parent
    except (ImportError, AttributeError):
        return Path(__file__).resolve().parents[2]


def _resolve_root(env_name: str, config_key: str, default: str) -> Path:
    """One of the two offline roots, absolute. See the module docstring.

    An absolute value is taken as-is; a relative one hangs off the monorepo
    root, so `data` means the same directory whether the batch was launched
    from cli-agents-master/ or from the repo root.
    """
    raw = os.environ.get(env_name) or repo_value("local", config_key) or default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (monorepo_root() / path)


def data_root(default: str = BUNDLE_PATH_PREFIX) -> Path:
    """Where the bundled benchmark inputs live (data/README.md).

    `default` is the benchmark preset's value, used when neither the
    environment nor config/config.yaml names one.
    """
    return _resolve_root(DATA_ROOT_ENV, "data_root", default)


def output_root(default: str = "outputs") -> Path:
    """Where an offline run writes its attempts (data/README.md `outputs/`)."""
    return _resolve_root(OUTPUT_ROOT_ENV, "output_root", default)


def describe_local_target(benchmark_defaults: Optional[Dict[str, str]] = None) -> str:
    """One log line naming the two offline roots a run will use, and why.

    The counterpart of describe_database_target: a batch that silently went
    offline (no database URL resolved) must say where it read and wrote
    instead, or an empty `outputs/` looks like a run that did nothing.
    """
    defaults = benchmark_defaults or {}
    src_in = (DATA_ROOT_ENV if os.environ.get(DATA_ROOT_ENV)
              else "config/config.yaml local.data_root" if repo_value("local", "data_root")
              else "default")
    src_out = (OUTPUT_ROOT_ENV if os.environ.get(OUTPUT_ROOT_ENV)
               else "config/config.yaml local.output_root" if repo_value("local", "output_root")
               else "default")
    din = data_root(defaults.get("data_root", BUNDLE_PATH_PREFIX))
    dout = output_root(defaults.get("output_root", "outputs"))
    return f"{din} (from {src_in}) -> {dout} (from {src_out})"


def resolve_bundled_path(rel_path: str, default_root: str = BUNDLE_PATH_PREFIX) -> Path:
    """Absolute path for one repo-relative path out of the bundle's JSON.

    Every path written into data/ is repo-relative and POSIX-separated
    (`data/tasks/task_id=1/starting_files/Foo.xlsx`). Joining it onto the
    monorepo root is the whole rule while data_root is the default; when it
    is not, the leading `data/` segment is the part that moves, so it is
    replaced rather than appended — otherwise a relocated bundle would
    resolve to <new root>/data/tasks/... and miss every file.
    """
    rel = PurePosixPath(str(rel_path).replace("\\", "/"))
    if rel.is_absolute():
        return Path(rel)
    parts = rel.parts
    if parts and parts[0] == BUNDLE_PATH_PREFIX:
        return data_root(default_root).joinpath(*parts[1:])
    return monorepo_root() / Path(*parts)


def to_repo_relative(path: Path) -> str:
    """POSIX, repo-relative spelling of a path a local run wrote.

    The JSONL rows and every `attempt_files` entry use this form so a bundle
    stays valid when the checkout moves. A path outside the repository (an
    output_root on another disk) has no repo-relative spelling and is kept
    absolute.
    """
    path = Path(path)
    try:
        return path.resolve().relative_to(monorepo_root().resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def resolve_attachments(rel_paths: Iterable[str]) -> List[Path]:
    """Absolute paths for monorepo-root-relative attachment paths.

    Raises at once on a missing or empty file: an attachment is prompt text
    the recorded prompt_version promises the agent saw, so running without
    it would be the empty-context defect all over again — refuse before any
    task is claimed.
    """
    root = monorepo_root()
    out: List[Path] = []
    for rel in rel_paths:
        path = root / rel
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(
                f"Prompt attachment {rel} is missing or empty under {root} "
                "(the prompt version declares it; it must exist before the "
                "batch starts)"
            )
        out.append(path)
    return out


def attachment_extra_configs(paths: Iterable[Path],
                             names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The provenance keys merged into task_attempts.extra_configs.

    House_Standards_v<n>.md -> house_standards: {version, file, sha256},
    the record every pipeline writes (house_standards/README.md). `file` is
    the name the agent saw in the workspace (v15+ delivers it as
    HOUSE_STANDARDS.md; `source` then keeps the versioned source name). The
    hash is computed at run time from the file actually shipped, not copied
    from a constant, so a silently edited file shows up as a different sha.
    """
    names = names or {}
    out: Dict[str, Any] = {}
    for path in paths:
        m = re.fullmatch(r"House_Standards_v(\d+)\.md", path.name)
        if not m:
            continue
        delivered = names.get(path.name, path.name)
        rec = {
            "version": int(m.group(1)),
            "file": delivered,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        if delivered != path.name:
            rec["source"] = path.name
        out["house_standards"] = rec
    return out
