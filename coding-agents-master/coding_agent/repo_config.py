"""Read the monorepo config at <SpreadsheetSmith>/config/config.yaml.

coding-agents-master is a workspace member of SpreadsheetSmith, whose root
pyproject.toml exposes config/python/config.py as the top-level `config`
module. That file is the single home for both benchmark database URLs
(database.v1_url / database.v2_url), the AWS credentials, the S3 bucket
name and the model API keys — so a run config's `benchmark:` key selects
the right database on its own, with no DATABASE_URL swapping in .env.

This is a deliberate copy of cli-agents-master/excel_cli_agent/repo_config.py
(kept separate so each package stays runnable on its own).

RESOLUTION ORDER (database url and AWS keys alike), first hit wins:

  1. the monorepo config — the only benchmark-aware layer.
  2. the environment (DATABASE_URL / boto3's default chain). This is the
     standalone-checkout path, where the `config` module does
     not exist and the env var is all there is.

Model API keys resolve the other way round: the environment (or .env) first,
then config/config.yaml keys.* — see config.resolve_api_key.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Points the monorepo-config lookup at a different directory. Used by the
# tests; also an escape hatch for layouts where the installed location is
# wrong. Unset -> Config resolves its own directory via the editable install.
REPO_CONFIG_DIR_ENV = "SPREADSHEETSMITH_CONFIG_DIR"

# Offline data layout (data/README.md). `local.data_root` holds the bundled
# benchmark inputs, `local.output_root` is where an offline run writes; both
# default to the repository's own data/ and outputs/ and are overridable per
# process by these env vars (an env value wins over config.yaml, so a
# reviewer can point a run at an unpacked archive without editing the file).
DATA_ROOT_ENV = "SPREADSHEETSMITH_DATA_ROOT"
OUTPUT_ROOT_ENV = "SPREADSHEETSMITH_OUTPUT_ROOT"
DEFAULT_DATA_ROOT = "data"
DEFAULT_OUTPUT_ROOT = "outputs"


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
    # keys coding-agents never reads (gemini_api_key, ...). Not worth a
    # warning to someone starting a run.
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


def monorepo_root() -> Optional[Path]:
    """<SpreadsheetSmith>, the directory the shared assets hang off (house_standards/
    ...), or None on a standalone checkout.

    The shared `config` module lives at <root>/config/python/config.py, so
    its DEFAULT_CONFIG_DIR is the authoritative locator — deliberately not
    SPREADSHEETSMITH_CONFIG_DIR, which redirects only the yaml lookup (tests point
    it at throwaway dirs that hold no assets). Without the workspace
    install, a checkout sitting directly under the monorepo still resolves
    through its parent.
    """
    try:
        from config import Config
    except ImportError:
        candidate = Path(__file__).resolve().parents[2]
        if (candidate / "config" / "config_default.yaml").exists():
            return candidate
        logger.debug("monorepo `config` module not installed and no repo above the checkout")
        return None
    return Path(Config.DEFAULT_CONFIG_DIR).resolve().parent


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


def database_name(url: str) -> str:
    """Database name out of a connection string, dropping every credential."""
    tail = url.split("://", 1)[-1].split("/", 1)
    if len(tail) < 2:
        return "?"
    return tail[1].split("?", 1)[0] or "?"


def describe_database_target(benchmark: Optional[str]) -> str:
    """One safe log line naming the DB a run will read/write, and why.

    Never includes the password — run logs are routinely pasted into
    issues and chats.
    """
    url, source = resolve_db_url(benchmark)
    if not url:
        return f"unresolved ({source})"
    return f"{database_name(url)} (from {source})"


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


def _local_root(env_var: str, key: str, default: str) -> Path:
    """A `local.*` root as an absolute path: env var, then config, then the
    default. A relative value resolves against the monorepo root (and, on a
    standalone checkout with no root to resolve against, the cwd)."""
    raw = os.environ.get(env_var) or repo_value("local", key) or default
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    root = monorepo_root()
    return (root / path) if root is not None else path.resolve()


def data_root() -> Path:
    """Where the bundled benchmark inputs live (`local.data_root`)."""
    return _local_root(DATA_ROOT_ENV, "data_root", DEFAULT_DATA_ROOT)


def output_root() -> Path:
    """Where an offline run writes its attempts (`local.output_root`)."""
    return _local_root(OUTPUT_ROOT_ENV, "output_root", DEFAULT_OUTPUT_ROOT)


def bundled_path(rel: str) -> Path:
    """Absolute path for a repo-relative path recorded in the bundle.

    Bundle JSON stores `data/tasks/task_id=1/...` whatever `local.data_root`
    says (data/README.md), so a non-default root replaces that leading
    segment rather than nesting under it."""
    root = data_root()
    parts = Path(rel).parts
    if parts and parts[0] == DEFAULT_DATA_ROOT:
        return root.joinpath(*parts[1:])
    return root / rel


def s3_client():
    """boto3 S3 client using config/config.yaml aws.* when both keys are set."""
    import boto3

    return boto3.client("s3", **boto3_credentials())
