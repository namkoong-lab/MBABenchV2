"""Read the monorepo config at <MBABenchV2>/config/config.yaml.

cli-agents-master is a workspace member of MBABenchV2, whose root
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
"""

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Points the monorepo-config lookup at a different directory. Used by the
# tests; also an escape hatch for layouts where the installed location is
# wrong. Unset -> Config resolves its own directory via the editable install.
REPO_CONFIG_DIR_ENV = "MBABENCH_CONFIG_DIR"


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
