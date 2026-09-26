"""AWS credentials and fleet identity, read from <repo>/config/config.yaml.

Every dispatcher command that touches EC2 goes through here, so they cannot
disagree about which account they are talking to. That is not tidiness: if
`spinup` creates a box in one account and `teardown` looks in another,
teardown reports "no matching instances" and exits 0 while the boxes keep
billing.

The credentials here are the same ones the boxes use for S3, which is what
keeps the fleet and its data in a single account.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# This module lives in dispatcher/helper/; .aws_defaults is operator state and
# stays in dispatcher/, so DISPATCHER_DIR is one level up from here.
DISPATCHER_DIR = Path(__file__).resolve().parents[1]
GUI_AGENTS_ROOT = DISPATCHER_DIR.parents[1]          # gui-agents-master
MONO_ROOT = GUI_AGENTS_ROOT.parent                   # <repo>
REPO_CONFIG = MONO_ROOT / "config" / "config.yaml"
AWS_DEFAULTS = DISPATCHER_DIR / ".aws_defaults"

if str(GUI_AGENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(GUI_AGENTS_ROOT))

logger = logging.getLogger("dispatch.aws")


class FleetConfigError(RuntimeError):
    """config.yaml is missing something EC2 work needs."""


@dataclass(frozen=True)
class FleetConfig:
    """The bits of <repo>/config/config.yaml the dispatcher provisions with."""

    access_key_id: str | None
    secret_access_key: str | None
    key_name: str | None
    sg_name: str | None
    ami: str | None

    @property
    def source(self) -> str:
        return f"{REPO_CONFIG} (aws.access_key_id={self.access_key_id})"


def require_config_module() -> None:
    """Fail loudly if this interpreter can't see the monorepo `config` module.

    Without it, ``_repo_value`` degrades to None by design — the right answer
    on a worker box, which never has the monorepo config. Here it is the wrong
    answer: every credential and fleet name would read as "unset" and the user
    would go hunting through config.yaml for values that are plainly there.

    The interpreter that does have it is the one named by `venv_path` in
    config.yaml.
    """
    try:
        import config  # noqa: F401
    except ImportError:
        venv = ""
        try:
            import yaml

            venv = (yaml.safe_load(REPO_CONFIG.read_text()) or {}).get("venv_path") or ""
        except Exception:  # noqa: BLE001 — this is already the error path
            pass
        hint = (
            f"    {venv}/bin/python -m infra.dispatcher.dispatch ...\n"
            if venv
            else "    (set venv_path in config.yaml)\n"
        )
        raise FleetConfigError(
            f"the monorepo `config` module is not importable by "
            f"{sys.executable}.\n"
            f"  Every value in {REPO_CONFIG} would read as unset. Run under "
            f"the environment named by venv_path:\n{hint}"
        ) from None


def _repo_aws(key: str) -> str | None:
    """One aws.* value from the monorepo config, or None.

    Goes through task_io.registry._repo_value rather than parsing the YAML:
    config_default.yaml ships ``${env:VAR}`` placeholders that only
    Config.load interpolates, and it deep-merges the defaults so keys added to
    config_default.yaml resolve against a config.yaml written before they
    existed. Reusing it also means the dispatcher and the runs themselves read
    credentials through exactly one code path.
    """
    from task_io.registry import _repo_value

    return _repo_value("aws", key)


def load_fleet_config() -> FleetConfig:
    require_config_module()
    if not REPO_CONFIG.is_file():
        raise FleetConfigError(
            f"not readable: {REPO_CONFIG}\n"
            f"  EC2 commands need it for AWS credentials, the key pair name "
            f"and the security group name.\n"
            f"  See config/config_default.yaml."
        )
    return FleetConfig(
        access_key_id=_repo_aws("access_key_id"),
        secret_access_key=_repo_aws("secret_access_key"),
        key_name=_repo_aws("gui_key_name"),
        sg_name=_repo_aws("gui_sg_name"),
        ami=_repo_aws("gui_ami"),
    )


# Credentials for every client this module hands out. Set by
# apply_credentials(); None means "nothing applied yet".
#
# Held explicitly rather than left to boto3's default session, which resolves
# its credentials ONCE and caches them for the life of the process. Setting
# os.environ afterwards has no effect on a session that already resolved — so
# any code path that built a client before apply_credentials() ran would pin
# the whole process to the ambient profile, and every later call would query
# the wrong account while looking like it worked. Passing the keys into an
# explicit Session makes each client's identity independent of call order.
_CREDS: tuple[str, str] | None = None


def apply_credentials(cfg: FleetConfig) -> None:
    """Adopt config.yaml's keys for every AWS client this module creates.

    Also exported to the environment, for the benefit of anything that shells
    out — but the exports are a convenience, not the mechanism. `_CREDS` is.

    AWS_PROFILE is cleared so output can't misdescribe what ran, and a stale
    AWS_SESSION_TOKEN is worse than cosmetic: pairing another account's token
    with these long-term keys fails as "security token invalid".
    """
    global _CREDS
    if not cfg.access_key_id or not cfg.secret_access_key:
        raise FleetConfigError(
            f"aws.access_key_id / aws.secret_access_key are unset in "
            f"{REPO_CONFIG}.\n"
            f"  They are the same credentials the boxes use for S3, which is "
            f"what keeps the fleet and its data in one account."
        )
    if os.environ.pop("AWS_PROFILE", None):
        logger.info("ignoring AWS_PROFILE (config.yaml credentials win)")
    if os.environ.pop("AWS_SESSION_TOKEN", None):
        logger.info("clearing inherited AWS_SESSION_TOKEN (keys are long-term)")
    os.environ["AWS_ACCESS_KEY_ID"] = cfg.access_key_id
    os.environ["AWS_SECRET_ACCESS_KEY"] = cfg.secret_access_key
    _CREDS = (cfg.access_key_id, cfg.secret_access_key)


def credentials_applied() -> bool:
    return _CREDS is not None


def _session(region: str):
    """A boto3 Session pinned to the config.yaml credentials when we have them."""
    import boto3

    if _CREDS is None:
        # Nothing applied yet — fall back to boto3's own chain so read-only
        # callers still work, and let the caller decide what an empty result
        # means. Deliberately not cached.
        return boto3.Session(region_name=region)
    return boto3.Session(
        aws_access_key_id=_CREDS[0],
        aws_secret_access_key=_CREDS[1],
        region_name=region,
    )


def ec2(region: str):
    return _session(region).client("ec2")


def sts(region: str):
    return _session(region).client("sts")


def account_id(region: str) -> str:
    return sts(region).get_caller_identity()["Account"]


def read_aws_defaults() -> dict[str, str]:
    """Parse the GUI_AGENTS_* assignments out of .aws_defaults.

    Plain parsing rather than sourcing it through bash: the file is a flat list
    of KEY="value" lines that bootstrap writes, and shelling out to read four
    strings is both slower and a way to execute whatever ends up in the file.
    Returns {} if it is missing or unreadable — callers treat that as "not
    bootstrapped yet", never as an error.
    """
    if not AWS_DEFAULTS.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        lines = AWS_DEFAULTS.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line.startswith("GUI_AGENTS_") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _expected_account() -> str | None:
    """GUI_AGENTS_AWS_ACCOUNT out of .aws_defaults, if bootstrap wrote one."""
    return read_aws_defaults().get("GUI_AGENTS_AWS_ACCOUNT") or None


def assert_expected_account(region: str, actual: str) -> None:
    """Refuse when the credentials point somewhere other than the account the
    key pair and security group actually live in.

    Used against the wrong account, run_instances fails with an opaque
    InvalidKeyPair.NotFound and describe_instances quietly returns nothing.
    No-op when either side is unknown, so a .aws_defaults written before this
    field existed keeps working.
    """
    expected = _expected_account()
    if not expected or not actual or expected == actual:
        return
    raise FleetConfigError(
        f"AWS account mismatch:\n"
        f"    credentials resolve to: {actual}\n"
        f"    .aws_defaults expects:  {expected}  (region={region})\n"
        f"  The key pair and security group live in {expected}; nothing the "
        f"dispatcher needs exists in {actual}.\n"
        f"  Fix aws.access_key_id in {REPO_CONFIG}, or re-run "
        f"dispatch bootstrap against the account you want."
    )


def connect(region: str, *, announce: bool = True) -> tuple[FleetConfig, str]:
    """Resolve config, apply credentials, verify the account. (cfg, account_id).

    The single entry point for every EC2-touching command.
    """
    cfg = load_fleet_config()
    apply_credentials(cfg)
    try:
        acct = account_id(region)
    except Exception as e:  # noqa: BLE001 — surface as a config problem
        raise FleetConfigError(
            f"could not verify AWS identity ({type(e).__name__}: {e}).\n"
            f"  Check aws.access_key_id / aws.secret_access_key in {REPO_CONFIG}."
        ) from e
    assert_expected_account(region, acct)
    if announce:
        print(f"  aws account: {acct}  (region={region}, creds from {REPO_CONFIG})")
    return cfg, acct


def lookup_sg_id(name: str, region: str) -> str | None:
    """Security group name -> id, or None if it doesn't exist here.

    Resolved live rather than read from .aws_defaults: the id is account- and
    region-specific, and a stale cache is how a box lands in a group that
    doesn't allow your IP.
    """
    resp = ec2(region).describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}]
    )
    groups = resp.get("SecurityGroups") or []
    return groups[0]["GroupId"] if groups else None


def describe_image(ami: str, region: str) -> str | None:
    """AMI name, or None if the id doesn't exist in this region.

    AMI ids are region-specific, so a config value carried to another region
    names nothing. Checking turns an opaque InvalidAMIID.NotFound at
    run_instances into a message that says which id and which region.
    """
    from botocore.exceptions import ClientError

    try:
        images = ec2(region).describe_images(ImageIds=[ami]).get("Images") or []
    except ClientError:
        return None
    return images[0].get("Name") if images else None


def set_repo_config_value(dotted: str, value: str) -> None:
    """Write one scalar into <repo>/config/config.yaml, e.g. "aws.gui_key_name".

    Line-based rather than a YAML round-trip: config.yaml holds live
    credentials, and re-emitting it through safe_dump would rewrite quoting and
    key order across the whole file just to add one scalar. Only the matched
    line changes; everything else stays byte-identical.
    """
    import json

    block, leaf = dotted.split(".", 1)
    lines = REPO_CONFIG.read_text().splitlines(keepends=True)

    start = next(
        (i for i, ln in enumerate(lines) if ln.rstrip("\n") == f"{block}:"), None
    )
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n{block}:\n  {leaf}: {json.dumps(value)}\n")
        REPO_CONFIG.write_text("".join(lines))
        return

    # The block runs to the next line starting in column 0.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i][0].isspace():
            end = i
            break
    indent = next(
        (ln[: len(ln) - len(ln.lstrip())] for ln in lines[start + 1 : end] if ln.strip()),
        "  ",
    )
    new = f"{indent}{leaf}: {json.dumps(value)}\n"
    for i in range(start + 1, end):
        if lines[i].strip().split(":", 1)[0] == leaf:
            lines[i] = new
            break
    else:
        # Insert after the block's last non-blank line, so a blank separator
        # between top-level blocks survives.
        last = max((i for i in range(start + 1, end) if lines[i].strip()), default=start)
        lines.insert(last + 1, new)
    REPO_CONFIG.write_text("".join(lines))


def repo_db_url(benchmark: str) -> str | None:
    """database.{v1,v2}_url from the monorepo config."""
    from task_io.registry import _repo_value

    return _repo_value("database", f"{benchmark}_url")


def database_name(url: str) -> str:
    """Database name out of a connection string, dropping every credential.

    Provisioning output is routinely pasted into chat; the password must not
    ride along with it.
    """
    tail = url.split("://", 1)[-1].split("/", 1)
    if len(tail) < 2:
        return "?"
    return tail[1].split("?", 1)[0] or "?"
