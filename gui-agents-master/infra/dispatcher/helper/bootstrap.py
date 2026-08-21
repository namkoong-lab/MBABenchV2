"""One-time AWS prerequisites: key pair + security group.

Idempotent — run it any time; it creates only what is missing, and
re-authorizes your current IP if it moved.

WHY CREDENTIALS COME FROM config.yaml AND NOT `aws configure`: the boxes write
their attempts to the S3 bucket owned by the account whose keys are in
config.yaml. Created under whatever ambient profile happened to be active, the
instances would land in one account and the data in another — split billing,
and `aws ec2 describe-instances` under the data account shows nothing. Sourcing
both from one file makes that mismatch unrepresentable.

The key pair and security group NAMES live in config.yaml too
(aws.gui_key_name / aws.gui_sg_name) rather than as flags or script defaults.
They are per-person and, once chosen, never change: the key pair is the only
way into a box (AWS cannot re-issue a lost private key), and the group is the
firewall the boxes sit behind. This prompts for them on first run and writes
the answers back, so it only ever asks once.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from infra.dispatcher.helper import aws_env
from infra.dispatcher.helper.aws_env import FleetConfigError

logger = logging.getLogger("dispatch.bootstrap")

CHECKIP_URL = "https://checkip.amazonaws.com"
SG_DESCRIPTION = "gui-agents boxes: SSH from operator IPs"
# Both names end up as AWS resource identifiers that every later run matches on
# exactly, so whitespace would turn each lookup into a silent miss and create a
# duplicate resource instead.
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _public_ip() -> str | None:
    """Current public IP. urlopen first, curl as fallback.

    Some macOS Python installs ship without a CA bundle and fail SSL
    verification against checkip; curl uses the system trust store.
    """
    from urllib.error import URLError
    from urllib.request import urlopen

    try:
        with urlopen(CHECKIP_URL, timeout=5) as resp:
            ip = resp.read().decode().strip()
            if ip:
                return ip
    except (URLError, TimeoutError, OSError):
        pass
    try:
        r = subprocess.run(
            ["curl", "-fsS", "--max-time", "5", CHECKIP_URL],
            capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _prompt_for_name(cfg_key: str, what: str) -> str:
    """Ask for a resource name. No default is offered: these name real AWS
    resources that every later run matches on exactly, and a name accepted by
    pressing Enter is one nobody chose."""
    if not sys.stdin.isatty():
        raise FleetConfigError(
            f"{cfg_key} is not set in {aws_env.REPO_CONFIG}, and this is not a "
            f"terminal.\n  Set {cfg_key} there and re-run."
        )
    print(f"\n{cfg_key} is not set in {aws_env.REPO_CONFIG}.")
    print(f"  {what}")
    try:
        name = input("  name: ").strip()
    except EOFError:
        name = ""
    if not NAME_RE.match(name):
        raise FleetConfigError(
            f"invalid name: {name!r} (letters, digits, . _ - only)\n"
            f"  Set {cfg_key} in {aws_env.REPO_CONFIG}, or re-run and type a "
            f"valid name."
        )
    return name


def _ensure_key_pair(client, key_name: str, region: str) -> Path:
    """Create the key pair if absent; return the local private-key path."""
    from botocore.exceptions import ClientError

    pem = Path.home() / ".ssh" / f"{key_name}.pem"
    print(f"\n── key pair '{key_name}' ──")
    try:
        client.describe_key_pairs(KeyNames=[key_name])
        exists = True
    except ClientError:
        exists = False

    if exists:
        print(f"  exists in AWS (region={region})")
        if pem.is_file():
            print(f"  private key already at {pem}")
        else:
            # AWS only ever hands out the private key once, at creation.
            print(f"  WARNING: {pem} is not on disk locally.")
            print("  AWS cannot re-send the private key, so any box launched")
            print("  with this key pair would be unreachable. Either:")
            print(f"    - copy your existing {key_name}.pem to {pem} (chmod 400), or")
            print("    - delete the key in AWS and re-run this command:")
            print(f"        aws ec2 delete-key-pair --region {region} "
                  f"--key-name {key_name}")
        return pem

    pem.parent.mkdir(parents=True, exist_ok=True)
    if pem.exists():
        raise FleetConfigError(
            f"AWS has no key named {key_name!r}, but {pem} already exists.\n"
            f"  Refusing to overwrite. Move it aside and re-run."
        )
    print(f"  creating key pair and saving private key to {pem}")
    material = client.create_key_pair(KeyName=key_name)["KeyMaterial"]
    pem.write_text(material)
    pem.chmod(stat.S_IRUSR)  # 0400 — ssh refuses a group/world-readable key
    print("  created.")
    return pem


def _ensure_security_group(
    client, sg_name: str, region: str, my_cidr: str, prune: bool
) -> str:
    """Create the group if absent and authorize SSH from `my_cidr`."""
    from botocore.exceptions import ClientError

    print(f"\n── security group '{sg_name}' ──")
    groups = client.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [sg_name]}]
    ).get("SecurityGroups") or []

    if groups:
        sg_id = groups[0]["GroupId"]
        print(f"  exists: {sg_id}")
    else:
        print("  creating")
        sg_id = client.create_security_group(
            GroupName=sg_name, Description=SG_DESCRIPTION
        )["GroupId"]
        client.create_tags(
            Resources=[sg_id],
            Tags=[{"Key": "Project", "Value": "gui-agents"},
                  {"Key": "Name", "Value": sg_name}],
        )
        print(f"  created: {sg_id}")

    if prune:
        print("  pruning existing SSH ingress rules…")
        current = client.describe_security_groups(GroupIds=[sg_id])[
            "SecurityGroups"][0].get("IpPermissions") or []
        for perm in current:
            if perm.get("FromPort") != 22 or perm.get("ToPort") != 22:
                continue
            for rng in perm.get("IpRanges") or []:
                cidr = rng.get("CidrIp")
                print(f"    revoking {cidr}")
                try:
                    client.revoke_security_group_ingress(
                        GroupId=sg_id, IpProtocol="tcp",
                        FromPort=22, ToPort=22, CidrIp=cidr,
                    )
                except ClientError as e:
                    logger.warning(f"    revoke {cidr} failed: {e}")

    print(f"  authorizing SSH from {my_cidr} (no-op if already present)")
    try:
        client.authorize_security_group_ingress(
            GroupId=sg_id, IpProtocol="tcp", FromPort=22, ToPort=22, CidrIp=my_cidr,
        )
    except ClientError as e:
        # Re-running with an unchanged IP is the normal case, not an error.
        if e.response.get("Error", {}).get("Code") != "InvalidPermission.Duplicate":
            logger.warning(f"    authorize failed: {e}")
    return sg_id


def _write_aws_defaults(region: str, key_name: str, sg_name: str,
                        sg_id: str, account: str) -> Path:
    """Record region + account for the other dispatch commands.

    They do NOT take the key/sg names from here — those come from config.yaml,
    and spinup resolves the sg-id from the name at launch rather than trusting
    the cached one below, which goes stale if the group is ever recreated. The
    names are recorded anyway so the reset script can clean up after a
    config.yaml change. No credentials: this file is defaults, not secrets.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    aws_env.AWS_DEFAULTS.write_text(
        f"# Written by `dispatch bootstrap` on {stamp}. Safe to edit.\n"
        f"GUI_AGENTS_REGION=\"{region}\"\n"
        f"GUI_AGENTS_KEY_NAME=\"{key_name}\"\n"
        f"GUI_AGENTS_SG_NAME=\"{sg_name}\"\n"
        f"GUI_AGENTS_SG_ID=\"{sg_id}\"\n"
        f"# The account the key pair and SG above live in. Recorded so the\n"
        f"# other commands refuse to run when their credentials resolve\n"
        f"# somewhere else — a keypair/SG from one account is meaningless in\n"
        f"# another, and the failure would otherwise surface as a confusing\n"
        f"# InvalidKeyPair.NotFound at run-instances.\n"
        f"GUI_AGENTS_AWS_ACCOUNT=\"{account}\"\n"
    )
    return aws_env.AWS_DEFAULTS


def cmd_bootstrap(args) -> int:
    region = args.region

    # Credentials. --ambient-creds exists for the chicken-and-egg case: a fresh
    # config.yaml with no keys in it yet, bootstrapping the account that will
    # hold them.
    cred_source = "ambient (--ambient-creds)"
    if args.ambient_creds:
        cfg = aws_env.load_fleet_config()
    else:
        try:
            cfg = aws_env.load_fleet_config()
            print(f"── AWS credentials from {aws_env.REPO_CONFIG} ──")
            aws_env.apply_credentials(cfg)
            cred_source = cfg.source
            print(f"  access_key_id: {cfg.access_key_id}")
        except FleetConfigError as e:
            logger.error(str(e))
            logger.error("  (or pass --ambient-creds to use the CLI's own chain)")
            return 2

    print("\n── verifying AWS credentials ──")
    try:
        # Through aws_env so it uses the credentials just applied, rather than
        # boto3's default session (which would resolve the ambient profile).
        ident = aws_env.sts(region).get_caller_identity()
    except Exception as e:  # noqa: BLE001
        logger.error(f"aws sts get-caller-identity failed: {e}")
        if not args.ambient_creds:
            logger.error(f"  check aws.access_key_id / aws.secret_access_key in "
                         f"{aws_env.REPO_CONFIG}")
        return 2
    account, arn = ident["Account"], ident["Arn"]
    print(f"  account: {account}")
    print(f"  arn:     {arn}")
    print(f"  region:  {region}")

    print("\n── detecting your public IP ──")
    ip = _public_ip()
    if not ip:
        logger.error("could not detect public IP")
        return 2
    my_cidr = f"{ip}/32"
    print(f"  public IP: {ip}")

    # Names: config.yaml, or a prompt. Anything prompted is written back after
    # the resources exist.
    persist: list[tuple[str, str]] = []
    try:
        key_name = cfg.key_name
        if not key_name:
            key_name = _prompt_for_name(
                "aws.gui_key_name",
                "Names the EC2 key pair — the only way to SSH into a box. AWS "
                "cannot re-issue a lost private key, so this stays fixed once "
                "chosen. Recommended: <you>-gui-agents",
            )
            persist.append(("aws.gui_key_name", key_name))
        sg_name = cfg.sg_name
        if not sg_name:
            sg_name = _prompt_for_name(
                "aws.gui_sg_name",
                "Names the security group — the firewall every box sits behind. "
                "Created here if absent, with port 22 open to your current IP. "
                "Recommended: <you>-gui-agents-sg",
            )
            persist.append(("aws.gui_sg_name", sg_name))
    except FleetConfigError as e:
        logger.error(str(e))
        return 2

    if not args.yes:
        print(f"\nAbout to create the key pair and security group in account "
              f"{account}.")
        print("Every box `dispatch spinup` launches will live there too, so this")
        print("must be the account that owns the S3 bucket the boxes write to.")
        try:
            answer = input(
                f"Proceed with key '{key_name}' and sg '{sg_name}' in "
                f"{region}? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("aborted")
            return 0

    client = aws_env.ec2(region)
    try:
        pem = _ensure_key_pair(client, key_name, region)
        sg_id = _ensure_security_group(client, sg_name, region, my_cidr,
                                       args.prune_ips)
    except FleetConfigError as e:
        logger.error(str(e))
        return 2

    # Persist only now that the resources actually exist: recording a name for
    # something that failed to create would make the next run skip the prompt
    # and then fail on a name that resolves to nothing.
    if persist:
        print(f"\n── recording names in {aws_env.REPO_CONFIG} ──")
        for dotted, value in persist:
            aws_env.set_repo_config_value(dotted, value)
            print(f"  {dotted}: {value}")

    defaults = _write_aws_defaults(region, key_name, sg_name, sg_id, account)
    print(f"  saved defaults to {defaults}")

    print("\n" + "=" * 56)
    print("AWS bootstrap complete.")
    print(f"  account:  {account}   ({arn})")
    print(f"  creds:    {cred_source}")
    print(f"  region:   {region}")
    print(f"  key-name: {key_name}   (private key: {pem})")
    print(f"  sg-name:  {sg_name}")
    print(f"  sg-id:    {sg_id}")
    print(f"  your IP:  {my_cidr} (allowed in {sg_name})")
    print("\nNext:")
    print("    dispatch spinup --alias <name> --config-template <file>")
    print("=" * 56)
    return 0
