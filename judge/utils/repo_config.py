"""Read the monorepo config at <MBABenchV2>/config/config.yaml.

judge/ is a workspace member of MBABenchV2, whose root pyproject.toml
exposes config/python/config.py as the top-level `config` module. That file
is the single home for both benchmark database URLs (database.v1_url /
database.v2_url), the AWS credentials, the S3 bucket name and the model API
keys — so `--benchmark v1|v2` selects the right database on its own, with no
DATABASE_URL swapping and no secrets in judge/.

This is a deliberate copy of coding-agents-master/coding_agent/repo_config.py
(kept separate so each package stays runnable on its own).

RESOLUTION ORDER (database url and AWS keys alike), first hit wins:

  1. the monorepo config — the only benchmark-aware layer.
  2. the environment (DATABASE_URL / boto3's default chain). This is the
     standalone-checkout path, where the `config` module does not exist and
     the env var is all there is.

Model API keys resolve the other way round: the environment first, then
config/config.yaml keys.* — see resolve_api_key.
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

    * Never raises: a standalone checkout has no config/ directory, so the
      import fails there — and that failure IS the signal to fall through to
      the environment, not an error.
    * Never writes: Config.load() defaults to create_missing=True, which
      seeds a config.yaml from the defaults as a side effect of reading one.
    * A `null` placeholder yields None, so an unset key falls through to the
      next layer rather than resolving to something falsy-but-present.
    """
    try:
        from config import Config
    except ImportError:
        logger.debug("monorepo `config` module not installed; using env vars")
        return None

    override = os.environ.get(REPO_CONFIG_DIR_ENV)
    # Config.load warns about every unset ${env:VAR} in the file, including
    # keys the judge never reads. Not worth a warning per run.
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


def s3_bucket(default: str = "mbabench") -> str:
    """config/config.yaml aws.s3_bucket, else `default`."""
    return repo_value("aws", "s3_bucket") or default


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


def s3_client():
    """boto3 S3 client using config/config.yaml aws.* when both keys are set."""
    import boto3

    return boto3.client("s3", **boto3_credentials())


# Provider -> (environment variable, config/config.yaml keys.* entry).
API_KEYS = {
    "openrouter": ("OPENROUTER_API_KEY", "openrouter_api_key"),
    "gemini": ("GEMINI_API_KEY", "gemini_api_key"),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    "openai": ("OPENAI_API_KEY", "openai_api_key"),
}


def resolve_api_key(provider: str, required: bool = True) -> Optional[str]:
    """API key for `provider`: environment first, then config keys.*.

    Env wins so a session-scoped key never has to be written to disk.
    """
    env_name, cfg_key = API_KEYS[provider]
    key = (os.environ.get(env_name) or "").strip() or repo_value("keys", cfg_key)
    if not key and required:
        raise EnvironmentError(
            f"No {provider} API key: set ${env_name} or keys.{cfg_key} in "
            f"<MBABenchV2>/config/config.yaml"
        )
    return key
