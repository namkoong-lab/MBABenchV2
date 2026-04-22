"""Upfront connectivity diagnostic for the dispatcher.

Runs before the SSH fan-out in `status` / `show` / `assign`. If the operator's
current public IP is not in the dispatcher security group's port-22 allowlist,
prints a warning pointing at aws_bootstrap.sh so they don't wait for SSH to
time out to discover the cause.

Never raises. Any failure (missing .aws_defaults, no AWS creds, offline, etc.)
falls back to either a generic hint or silence — the caller's normal error
path still runs.

Disabled by `DISPATCH_NO_DIAGNOSE=1`.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


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

_DEFAULTS_PATH = Path(__file__).resolve().parent / ".aws_defaults"
_BOOTSTRAP_REL = "./infra/dispatcher/aws_bootstrap.sh"
_CHECKIP_URL = "https://checkip.amazonaws.com"
_HTTP_TIMEOUT = 3
_AWS_TIMEOUT = 8


def _read_aws_defaults() -> dict[str, str] | None:
    """Source .aws_defaults via bash and return the exported vars.

    Matches how spinup.sh / teardown.sh consume the same file. Returns None
    if the file is missing or bash fails."""
    if not _DEFAULTS_PATH.exists():
        return None
    script = (
        f'set -e; source "{_DEFAULTS_PATH}"; '
        'printf "%s\\n%s\\n%s\\n" '
        '"${GUI_AGENTS_REGION:-}" "${GUI_AGENTS_SG_ID:-}" "${GUI_AGENTS_SG_NAME:-}"'
    )
    try:
        r = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    lines = r.stdout.splitlines()
    if len(lines) < 3:
        return None
    region, sg_id, sg_name = lines[0].strip(), lines[1].strip(), lines[2].strip()
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
    """Return the list of IPv4 CIDRs allowed on port 22, or None on failure."""
    try:
        r = subprocess.run(
            [
                "aws", "ec2", "describe-security-groups",
                "--region", region,
                "--group-ids", sg_id,
                "--query",
                "SecurityGroups[0].IpPermissions[?FromPort==`22` && ToPort==`22`].IpRanges[].CidrIp",
                "--output", "json",
            ],
            capture_output=True,
            text=True,
            timeout=_AWS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [str(c) for c in data]


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

    The SG is shared across all boxes (written by aws_bootstrap.sh), so one
    lookup covers the whole fleet."""
    if os.environ.get("DISPATCH_NO_DIAGNOSE") == "1":
        return ConnectivityVerdict.UNKNOWN

    defaults = _read_aws_defaults()
    if defaults is None:
        # No .aws_defaults — operator may not have run aws_bootstrap.sh. Stay
        # silent; the bootstrap script isn't mandatory in all deployments.
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
