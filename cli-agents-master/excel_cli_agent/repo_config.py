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

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


def monorepo_root() -> Path:
    """<MBABenchV2>, the directory holding config/ and house_standards/.

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
