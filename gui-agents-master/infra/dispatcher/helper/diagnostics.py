"""Upfront connectivity diagnostic for the dispatcher.

Runs before the SSH fan-out in `status` / `show` / `assign`. If the operator's
current public IP is not in the dispatcher security group's port-22 allowlist,
prints a warning pointing at `dispatch bootstrap` so they don't wait for SSH
to time out to discover the cause.

Never raises. Any failure (missing .aws_defaults, no AWS creds, offline, etc.)
falls back to either a generic hint or silence — the caller's normal error
path still runs.

Disabled by `DISPATCH_NO_DIAGNOSE=1`.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess  # only for the curl fallback in _current_public_ip
import sys
from urllib.error import URLError
from urllib.request import urlopen

from infra.dispatcher.helper import aws_env


class ConnectivityVerdict(enum.Enum):
    """Return type for `diagnose_connectivity`.

    OK         — current public IP is in the dispatcher SG's port-22 allowlist.
    UNKNOWN    — could not determine (no .aws_defaults, no AWS creds, offline,
                 diagnostic disabled via DISPATCH_NO_DIAGNOSE=1, etc.). Callers
                 should proceed as if no check ran.
    IP_BLOCKED — current public IP is definitively NOT in the allowlist.
                 Callers should skip the SSH fan-out and exit non-zero.
    """

    OK = "ok"
    UNKNOWN = "unknown"
    IP_BLOCKED = "ip_blocked"

logger = logging.getLogger("dispatch.diagnostics")

_BOOTSTRAP_REL = "dispatch bootstrap"
_CHECKIP_URL = "https://checkip.amazonaws.com"
_HTTP_TIMEOUT = 3


def _read_aws_defaults() -> dict[str, str] | None:
    """Region + security group to check, or None if we can't tell.

    The group NAME comes from config.yaml when it is set there, and is
    resolved to an id live — that is what every other command does, and a
    cached id in .aws_defaults goes stale the moment the group is recreated.
    .aws_defaults supplies the region, and the id as a fallback for a checkout
    bootstrapped before the names moved into config.yaml.
    """
    defaults = aws_env.read_aws_defaults()
    region = defaults.get("GUI_AGENTS_REGION") or ""
    sg_id = defaults.get("GUI_AGENTS_SG_ID") or ""
    sg_name = defaults.get("GUI_AGENTS_SG_NAME") or ""

    # Only resolve the name against AWS once the config.yaml credentials are in
    # play; querying under the ambient profile would look up the group in some
    # other account and silently fall back to the cached id.
    if not aws_env.credentials_applied():
        return {"region": region, "sg_id": sg_id, "sg_name": sg_name} \
            if region and sg_id else None
    try:
        cfg = aws_env.load_fleet_config()
        if cfg.sg_name:
            sg_name = cfg.sg_name
            resolved = aws_env.lookup_sg_id(sg_name, region) if region else None
            if resolved:
                sg_id = resolved
    except Exception as e:  # noqa: BLE001 — diagnostics never raises
        logger.debug("could not resolve sg from config.yaml: %s", e)

    if not region or not sg_id:
        return None
    return {"region": region, "sg_id": sg_id, "sg_name": sg_name}


def _current_public_ip() -> str | None:
    # urlopen first; fall back to curl since some Python installs on macOS
    # ship without a CA bundle and fail SSL verification against
    # checkip.amazonaws.com. curl uses the system trust store.
    try:
        with urlopen(_CHECKIP_URL, timeout=_HTTP_TIMEOUT) as resp:
            ip = resp.read().decode().strip()
            if ip:
                return ip
    except (URLError, TimeoutError, OSError):
        pass
    try:
        r = subprocess.run(
            ["curl", "-fsS", "--max-time", str(_HTTP_TIMEOUT), _CHECKIP_URL],
            capture_output=True,
            text=True,
            timeout=_HTTP_TIMEOUT + 1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _sg_port22_cidrs(region: str, sg_id: str) -> list[str] | None:
    """IPv4 CIDRs allowed on port 22, or None if we couldn't find out.

    Goes through boto3 under the config.yaml credentials rather than shelling
    out to `aws`, which would use whatever ambient profile happened to be
    active. That is the same account mix-up the rest of the dispatcher exists
    to prevent — and here it produced a false "could not verify" warning on
    every run, because the ambient profile can't see the group at all.
    """
    try:
        resp = aws_env.ec2(region).describe_security_groups(GroupIds=[sg_id])
    except Exception as e:  # noqa: BLE001 — diagnostics never raises
        logger.debug("describe_security_groups failed: %s", e)
        return None
    groups = resp.get("SecurityGroups") or []
    if not groups:
        return None
    cidrs: list[str] = []
    for perm in groups[0].get("IpPermissions") or []:
        if perm.get("FromPort") != 22 or perm.get("ToPort") != 22:
            continue
        for rng in perm.get("IpRanges") or []:
            if rng.get("CidrIp"):
                cidrs.append(str(rng["CidrIp"]))
    return cidrs


def _print_generic_hint() -> None:
    print(
        "WARNING: could not verify dispatcher SG allows your IP.\n"
        f"  If boxes come back UNREACHABLE, re-run: {_BOOTSTRAP_REL} -y",
        file=sys.stderr,
    )


def diagnose_connectivity() -> ConnectivityVerdict:
    """Check whether the current public IP is in the dispatcher SG's port-22
    allowlist. Warns on stderr if it isn't, and returns a verdict the caller
    can use to decide whether to short-circuit the SSH fan-out.

    The SG is shared across all boxes (written by `dispatch bootstrap`), so
    one lookup covers the whole fleet."""
    if os.environ.get("DISPATCH_NO_DIAGNOSE") == "1":
        return ConnectivityVerdict.UNKNOWN

    # Put the config.yaml credentials into the environment before any AWS call
    # below, so this check runs against the same account the boxes live in.
    # Failure is not fatal here: `status` and friends work fine over SSH alone,
    # and this is only a courtesy warning.
    try:
        aws_env.apply_credentials(aws_env.load_fleet_config())
    except Exception as e:  # noqa: BLE001 — diagnostics never raises
        logger.debug("no config.yaml credentials for the SG check: %s", e)
        return ConnectivityVerdict.UNKNOWN

    defaults = _read_aws_defaults()
    if defaults is None:
        # No .aws_defaults — operator may not have run `dispatch bootstrap`.
        # Stay silent; bootstrap isn't mandatory in all deployments.
        return ConnectivityVerdict.UNKNOWN

    ip = _current_public_ip()
    if ip is None:
        # Offline or checkip rate-limited — nothing useful to say.
        return ConnectivityVerdict.UNKNOWN

    cidrs = _sg_port22_cidrs(defaults["region"], defaults["sg_id"])
    if cidrs is None:
        _print_generic_hint()
        return ConnectivityVerdict.UNKNOWN

    my_cidr = f"{ip}/32"
    if my_cidr in cidrs:
        return ConnectivityVerdict.OK

    allowed = ", ".join(cidrs) if cidrs else "(none)"
    sg_label = defaults.get("sg_name") or defaults["sg_id"]
    print(
        "WARNING: your public IP is not in the dispatcher security group.\n"
        f"  your public IP: {ip}\n"
        f"  SG {sg_label} ({defaults['sg_id']}) allows SSH from: {allowed}\n"
        "  Boxes will come back UNREACHABLE until you re-authorize. Fix with:\n"
        f"      {_BOOTSTRAP_REL} --region {defaults['region']} -y",
        file=sys.stderr,
    )
    return ConnectivityVerdict.IP_BLOCKED
