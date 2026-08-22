"""EC2 lifecycle for gui-agents boxes: spinup / stop / teardown.

One tool owns the whole box lifecycle, and one code path resolves
credentials (see aws_env.py).

The three verbs differ in what survives:

    spinup    launch a box, or re-provision one that already exists. On a
              STOPPED instance it starts it again rather than launching a
              second one.
    stop      halt the instance. Compute billing stops; the EBS root volume
              survives, so the Chrome profile and its logged-in cookies are
              still there when it starts again. The root volume keeps billing
              as storage.
    teardown  terminate. The volume is destroyed with it, so the browser login
              is gone and the box must be provisioned and logged in afresh.

Between runs you want `stop`. `teardown` is for when the storage cost should
stop too.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from infra.dispatcher.helper import aws_env, boxes as boxes_mod
from infra.dispatcher.helper.aws_env import FleetConfigError

logger = logging.getLogger("dispatch.provision")

REPO_ROOT = aws_env.GUI_AGENTS_ROOT
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]

# Sizes offered by the spinup prompt. Both are 2 vCPU burstable; they differ in
# RAM and in burst baseline (t3.medium 20%, t3.large 30%).
INSTANCE_TYPES = ("t3.medium", "t3.large")
DEFAULT_INSTANCE_TYPE = "t3.large"
INSTANCE_TYPE_SPECS = {
    "t3.medium": "2 vCPU,  4 GiB RAM,  ~$0.042/hr  — see warning above",
    "t3.large":  "2 vCPU,  8 GiB RAM,  ~$0.083/hr  — recommended",
}
# 2026-08-22: chatgpt-sol56-work-1 (t3.medium) wedged ~4h20m into a ChatGPT run.
# The kernel OOM-killed Chrome at 3.2 GiB RSS, and the host then swap-thrashed
# hard enough that sshd accepted TCP but never sent a banner — the box was
# unreachable over SSH and had to be recovered through the EC2 API. It had also
# been sitting at zero CPU credits, running on surplus. t3.large fixes both:
# 8 GiB of RAM, and a 30% baseline that this workload stays under.
OOM_WARNING = """\
  ⚠  t3.medium OOMs on ChatGPT-in-Chrome runs (observed 2026-08-22)
     Chrome reached 3.2 GiB RSS on a 4 GiB box; the OOM killer fired and the
     host swap-thrashed until sshd stopped answering. Recovery needed an API
     reboot. Pick t3.large for ChatGPT boxes."""

# cloud-init installs the base packages so the box is usable the moment its
# state goes to 'running'. Chrome is the heavyweight; the rest are small.
USER_DATA = """#cloud-config
package_update: true
package_upgrade: false
packages:
  - python3
  - python3-pip
  - git
  - rsync
  - xvfb
  - x11vnc
  - tmux
  - wget
  - ca-certificates
  - gnupg
runcmd:
  - wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  - apt-get install -y /tmp/chrome.deb
  - rm -f /tmp/chrome.deb
  - mkdir -p /var/lib/gui-agents
  - mkdir -p /etc/gui-agents
  - touch /var/lib/gui-agents/.bootstrap-done
"""

SYSTEMD_UNITS = [
    "xvfb.service",
    "gui-agents-chrome.service",
    "gui-agents-worker.service",
    "gui-agents-auth-probe.service",
    "gui-agents-auth-probe.timer",
]


# ---------------------------------------------------------------------------
# SSH / rsync
# ---------------------------------------------------------------------------


def _key_path(key_name: str) -> Path:
    return Path.home() / ".ssh" / f"{key_name}.pem"


def _ssh_base(key_name: str) -> list[str]:
    return ["-i", str(_key_path(key_name)), *SSH_OPTS]


def ssh_run(
    host: str,
    key_name: str,
    cmd: str,
    *,
    check: bool = True,
    timeout: int = 900,
    retries: int = 3,
) -> subprocess.CompletedProcess:
    """Run one command over SSH, retrying connection-level failures.

    A freshly booted or just-restarted instance answers SSH before its
    networking has fully settled, so an occasional connect timeout mid-setup
    is normal and not a real failure. ssh reports those as exit 255, which is
    distinguishable from the remote command's own non-zero exit — so only 255
    is retried, and a genuine command failure still surfaces immediately.
    """
    import time

    proc = None
    for attempt in range(retries):
        proc = subprocess.run(
            ["ssh", *_ssh_base(key_name), f"ubuntu@{host}", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 255:
            break
        if attempt < retries - 1:
            logger.warning(
                f"ssh {host}: connection failed (attempt {attempt + 1}/{retries}), "
                f"retrying…"
            )
            time.sleep(5)
    assert proc is not None
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"ssh {host}: `{cmd}` failed ({proc.returncode})\n"
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc


def scp_to(host: str, key_name: str, src: Path, dst: str) -> None:
    proc = subprocess.run(
        ["scp", *_ssh_base(key_name), str(src), f"ubuntu@{host}:{dst}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"scp to {host} failed: {proc.stderr.strip()}")


def wait_for(desc: str, probe, tries: int = 60, delay: int = 5) -> None:
    import time

    print(f"Waiting for {desc}…")
    for _ in range(tries):
        if probe():
            return
        time.sleep(delay)
    raise RuntimeError(f"timed out waiting for {desc}")


# ---------------------------------------------------------------------------
# Template introspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateInfo:
    provider: str
    model_name: str
    agent_folder: str
    agent_model_type: str
    benchmark: str


def read_template(path: Path) -> TemplateInfo:
    """Resolve provider, agent identity and benchmark from a config template.

    Runs the real load_configs + identity resolver, so a template with a
    missing behavior field (no chatgpt_web block, an unknown model/effort
    combination) fails here rather than after an instance is already running.

    `benchmark` comes from the template rather than a flag on purpose: a box is
    pinned to one experiment, the template already names it through
    source.schema / sink.schema, and a second switch typed by hand is how a box
    ends up writing one experiment's attempts into the other's database.
    """
    from infra.configs import load_configs, resolve_agent_identity

    cfg = load_configs(override_path=path)

    provider = getattr(getattr(cfg, "provider", None), "kind", "") or ""
    if provider not in ("claude", "chatgpt"):
        raise FleetConfigError(
            f"provider.kind in {path} must be 'claude' or 'chatgpt', got {provider!r}"
        )

    benchmark = (getattr(cfg, "benchmark", "") or "").strip().lower()
    if benchmark not in ("v1", "v2"):
        raise FleetConfigError(
            f"benchmark in {path} must be 'v1' or 'v2', got {benchmark!r}"
        )

    identity = resolve_agent_identity(cfg)
    return TemplateInfo(
        provider=provider,
        model_name=identity.model_name,
        agent_folder=identity.agent_folder,
        agent_model_type=identity.agent_model_type,
        benchmark=benchmark,
    )


# ---------------------------------------------------------------------------
# Box setup (rsync + deps + units + secrets)
# ---------------------------------------------------------------------------


def _project_dependencies() -> list[str]:
    """[project].dependencies out of gui-agents-master/pyproject.toml.

    The box gets the project's DEPENDENCIES, not the project. The worker
    reaches the code through PYTHONPATH=/opt/gui-agents-master (see the
    systemd unit), so the package never needs to be importable from
    site-packages — and installing it would fail anyway: pyproject.toml
    declares requires-python >=3.12 while the Ubuntu 22.04 AMI ships 3.10.

    Read from pyproject rather than restated here so there is one list. (The
    old requirements.txt was deleted with the rest of the env setup files.)
    """
    import tomllib

    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    if not deps:
        raise RuntimeError(f"no [project].dependencies in {REPO_ROOT}/pyproject.toml")
    return deps


def _write_secrets_env(db_url: str, cfg: aws_env.FleetConfig) -> Path:
    """Build the secrets.env the worker's systemd unit reads.

    The env var NAMES here are what the box resolves through
    database.url_env / aws.*_env — boxes never see the monorepo config, since
    rsync only sends gui-agents-master. That env var is a single blind url
    that cannot tell v1 from v2, which is why the benchmark selection happens
    once, here, at provisioning time.
    """
    fd, name = tempfile.mkstemp(prefix="gui-agents-secrets.")
    path = Path(name)
    os.close(fd)
    path.chmod(0o600)
    lines = [
        f"MBABENCHV2JUDGE_KEYS_DATABASE_URL={db_url}",
        f"AWS_ACCESS_KEY_ID={cfg.access_key_id}",
        f"AWS_SECRET_ACCESS_KEY={cfg.secret_access_key}",
    ]
    # No monorepo equivalent — long-term keys are the norm. Carried through
    # only if the operator's shell happens to have one.
    token = os.environ.get("AWS_SESSION_TOKEN")
    if token:
        lines.append(f"AWS_SESSION_TOKEN={token}")
    path.write_text("\n".join(lines) + "\n")
    return path


def push_setup(
    host: str,
    *,
    cfg: aws_env.FleetConfig,
    template: Path,
    db_url: str,
    restart: bool,
) -> None:
    """rsync the repo, install deps + units, push secrets/config, start services."""
    key = cfg.key_name
    assert key

    print(f"── rsync {REPO_ROOT} → ubuntu@{host}:/opt/gui-agents-master ──")
    # --rsync-path="sudo rsync" lets the unprivileged 'ubuntu' user write into
    # /opt (it has NOPASSWD sudo on the AWS Ubuntu AMI). --no-owner --no-group
    # cancels -a's implicit owner/group preservation, so files land root:root
    # rather than carrying the laptop's uid.
    rsync = subprocess.run(
        [
            "rsync", "-az", "--delete",
            "--rsync-path=sudo rsync",
            "--no-owner", "--no-group",
            "--filter=:- .gitignore",
            "--exclude=.git/",
            "-e", "ssh " + " ".join(_ssh_base(key)),
            f"{REPO_ROOT}/",
            f"ubuntu@{host}:/opt/gui-agents-master/",
        ],
        capture_output=True,
        text=True,
    )
    if rsync.returncode != 0:
        raise RuntimeError(f"rsync failed: {rsync.stderr.strip()}")

    # Normalize perms explicitly: rsync's --chmod doesn't reliably update an
    # existing top-level dir, and a macOS source under /Users can be 750, which
    # would leave /opt/gui-agents-master untraversable for the ssh user.
    ssh_run(host, key, "sudo chown -R root:root /opt/gui-agents-master "
                       "&& sudo chmod -R a+rX /opt/gui-agents-master")

    print("── pip install dependencies ──")
    deps = " ".join(shlex.quote(d) for d in _project_dependencies())
    ssh_run(host, key, f"sudo pip3 install -q {deps}")

    print("── install queue CLI + systemd units ──")
    ssh_run(host, key, "sudo install -m 0755 "
                       "/opt/gui-agents-master/infra/worker/systemd/gui-agents-queue "
                       "/usr/local/bin/gui-agents-queue")
    for unit in SYSTEMD_UNITS:
        ssh_run(host, key, f"sudo install -m 0644 "
                           f"/opt/gui-agents-master/infra/worker/systemd/{unit} "
                           f"/etc/systemd/system/{unit}")
    # Uncomment the EnvironmentFile line in the unit we just installed. The
    # pattern must tolerate the comment indent the unit file actually uses
    # ("#   EnvironmentFile=..."); an exact "^# EnvironmentFile=" matches
    # nothing, and a sed that matches nothing still exits 0 — the worker would
    # come up with no DB url and no AWS credentials, and the only symptom
    # would be tasks failing much later. Verified below rather than trusted.
    ssh_run(host, key, "sudo sed -i 's|^#[[:space:]]*EnvironmentFile=|EnvironmentFile=|' "
                       "/etc/systemd/system/gui-agents-worker.service")
    ssh_run(host, key, "sudo install -d -m 0755 /etc/gui-agents")
    ssh_run(host, key, "sudo install -d -m 0777 /var/lib/gui-agents")
    # state.json was created root:root 0644 on older boxes; relax it so the
    # ubuntu ssh user can open it O_RDWR through gui-agents-queue.
    ssh_run(host, key, "sudo test -e /var/lib/gui-agents/state.json "
                       "&& sudo chmod 666 /var/lib/gui-agents/state.json || true")

    print("── push secrets.env ──")
    secrets = _write_secrets_env(db_url, cfg)
    try:
        scp_to(host, key, secrets, "/tmp/gui-agents-secrets.env")
        ssh_run(host, key, "sudo install -m 0600 -o root -g root "
                           "/tmp/gui-agents-secrets.env /etc/gui-agents/secrets.env "
                           "&& rm -f /tmp/gui-agents-secrets.env")
    finally:
        secrets.unlink(missing_ok=True)

    print("── push configs.yaml ──")
    scp_to(host, key, template, "/tmp/gui-agents-configs.yaml")
    ssh_run(host, key, "sudo install -m 0644 /tmp/gui-agents-configs.yaml "
                       "/opt/gui-agents-master/infra/configs/configs.yaml "
                       "&& rm -f /tmp/gui-agents-configs.yaml")

    action = "restart" if restart else "enable"
    print(f"── {action} gui-agents-worker.service ──")
    ssh_run(host, key, "sudo systemctl daemon-reload")

    # Confirm the sed above actually uncommented EnvironmentFile. Without it
    # the worker starts happily and only fails once a task reaches the DB or
    # S3, by which point the cause is a long way from the symptom.
    # Note the plural: systemd's property is EnvironmentFiles, and asking for
    # a property that doesn't exist returns empty rather than erroring — so
    # the singular form reads exactly like "the sed failed".
    envfile = ssh_run(
        host, key,
        "systemctl show gui-agents-worker.service -p EnvironmentFiles --value",
    ).stdout.strip()
    if not envfile:
        raise RuntimeError(
            "gui-agents-worker.service has no EnvironmentFile after "
            "daemon-reload.\n  /etc/gui-agents/secrets.env would be ignored "
            "and the worker would run with no database url and no AWS "
            "credentials."
        )
    print(f"  EnvironmentFile: {envfile}")

    ssh_run(host, key, "sudo systemctl enable --now xvfb.service")
    # Chrome lives in its own service so cookies survive worker/task cgroup
    # teardowns. On re-runs restart it, so it picks up any new
    # provider/profile_dir from the configs.yaml we just pushed.
    for unit in ("gui-agents-chrome.service", "gui-agents-worker.service"):
        verb = "restart" if restart else "enable --now"
        ssh_run(host, key, f"sudo systemctl {verb} {unit}")
    # Auth-probe timer: enable on first spinup; on re-runs restart it, since
    # daemon-reload alone doesn't reschedule an already-active timer.
    ssh_run(
        host, key,
        "sudo systemctl "
        + ("restart" if restart else "enable --now")
        + " gui-agents-auth-probe.timer",
    )

    for unit in ("gui-agents-chrome.service", "gui-agents-worker.service"):
        if ssh_run(host, key, f"sudo systemctl is-active --quiet {unit}",
                   check=False).returncode != 0:
            status = ssh_run(host, key, f"sudo systemctl status --no-pager {unit}",
                             check=False).stdout
            raise RuntimeError(f"{unit} is not active after {action}\n{status}")


def _remote_is_idle(host: str, key_name: str) -> bool:
    """True when the worker has no current task and an empty queue.

    If gui-agents-queue can't produce valid JSON (not installed, or a prior
    half-finished setup left it broken) treat the box as idle, so a re-run can
    finish the bootstrap instead of being locked out by it.
    """
    import json

    proc = ssh_run(host, key_name, "gui-agents-queue show 2>/dev/null",
                   check=False, timeout=30)
    if proc.returncode != 0 or not proc.stdout.strip():
        print("  (gui-agents-queue returned no output — treating as idle for re-setup)")
        return True
    try:
        state = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return True
    return state.get("current") is None and not (state.get("queue") or [])


def _current_task(host: str, key_name: str) -> str | None:
    """task_id of whatever the worker is running, or None."""
    import json

    proc = ssh_run(host, key_name, "gui-agents-queue show 2>/dev/null",
                   check=False, timeout=30)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        current = json.loads(proc.stdout).get("current")
    except json.JSONDecodeError:
        return None
    return str(current.get("task_id")) if current else None


# ---------------------------------------------------------------------------
# spinup
# ---------------------------------------------------------------------------


def _resolve_instance_type(args) -> str:
    """Resolve --instance-type, prompting when it was omitted.

    An explicit --instance-type is honoured as given (any type, not just the
    two offered by the prompt) so the fleet can still be sized off-menu.
    """
    if args.instance_type:
        if args.instance_type == "t3.medium":
            print("\n" + OOM_WARNING)
        return args.instance_type

    if args.yes or not sys.stdin.isatty():
        print(f"\ninstance type: {DEFAULT_INSTANCE_TYPE}  "
              f"(not prompting: {'-y' if args.yes else 'no TTY'}; "
              f"pass --instance-type to choose)")
        return DEFAULT_INSTANCE_TYPE

    print("\n" + OOM_WARNING)
    print("\nInstance type:")
    for i, t in enumerate(INSTANCE_TYPES, 1):
        mark = " [default]" if t == DEFAULT_INSTANCE_TYPE else ""
        print(f"  {i}) {t:<11}{INSTANCE_TYPE_SPECS[t]}{mark}")
    default_idx = INSTANCE_TYPES.index(DEFAULT_INSTANCE_TYPE) + 1
    while True:
        choice = input(f"Choose [1-{len(INSTANCE_TYPES)}] "
                       f"(default {default_idx}): ").strip()
        if not choice:
            return DEFAULT_INSTANCE_TYPE
        if choice.isdigit() and 1 <= int(choice) <= len(INSTANCE_TYPES):
            return INSTANCE_TYPES[int(choice) - 1]
        if choice in INSTANCE_TYPES:
            return choice
        print(f"  not a choice: {choice!r}")


def cmd_spinup(args) -> int:
    region = args.region
    template = Path(args.config_template)
    if not template.is_file():
        logger.error(f"--config-template not readable: {template}")
        return 2

    try:
        cfg, account = aws_env.connect(region)
    except FleetConfigError as e:
        logger.error(str(e))
        return 2

    if not cfg.key_name or not cfg.sg_name:
        logger.error(
            f"aws.gui_key_name / aws.gui_sg_name are not set in "
            f"{aws_env.REPO_CONFIG}.\n"
            f"  Run dispatch bootstrap — it prompts for the names, "
            f"creates the key pair and security group, and saves both."
        )
        return 2

    # Resolve the group name to an id live rather than trusting a cached one:
    # the id is account- and region-specific, and a stale cache is how a box
    # lands in a security group that doesn't allow your IP.
    sg_id = aws_env.lookup_sg_id(cfg.sg_name, region)
    if not sg_id:
        logger.error(
            f"security group {cfg.sg_name!r} not found in account {account} "
            f"(region={region}).\n  Run dispatch bootstrap to create it."
        )
        return 2
    print(f"  key pair: {cfg.key_name}")
    print(f"  sg:       {cfg.sg_name} ({sg_id})")

    if not _key_path(cfg.key_name).is_file():
        logger.error(
            f"private key not found: {_key_path(cfg.key_name)}\n"
            f"  AWS cannot re-issue it. A box launched now could never be "
            f"reached.\n  Run dispatch bootstrap (delete the AWS "
            f"key pair first if it still exists)."
        )
        return 2

    try:
        info = read_template(template)
    except FleetConfigError as e:
        logger.error(str(e))
        return 2
    print("\nresolved agent identity:")
    print(f"  model_name:       {info.model_name}   (→ task_attempts.agent_model_name)")
    print(f"  agent_folder:     {info.agent_folder}   (→ S3 prefix segment)")
    print(f"  agent_model_type: {info.agent_model_type}   (→ task_attempts.agent_model_type)")
    print(f"  benchmark:        {info.benchmark}   (→ database.{info.benchmark}_url)")

    db_url = aws_env.repo_db_url(info.benchmark)
    if not db_url:
        logger.error(
            f"database.{info.benchmark}_url is not set in {aws_env.REPO_CONFIG}.\n"
            f"  The template says benchmark: {info.benchmark}, so that is the "
            f"database this box needs."
        )
        return 2
    print(f"  database:         {aws_env.database_name(db_url)}   "
          f"(from database.{info.benchmark}_url)")

    existing = None
    try:
        existing = boxes_mod.find_by_alias(args.alias)
    except (KeyError, FileNotFoundError):
        pass

    # Prompt last: everything above can reject the run without costing money,
    # and there is no point asking for a size before we know the run is valid.
    args.instance_type = _resolve_instance_type(args)

    if existing is not None:
        return _respin(existing, args, cfg, account, template, db_url, region)
    return _launch(args, cfg, account, sg_id, template, db_url, info, region)


def _respin(box, args, cfg, account, template, db_url, region) -> int:
    """Re-provision an already-registered box, starting it first if stopped."""
    print(f"\nAlias {box.alias!r} already registered — re-run path.")
    if not box.instance_id:
        logger.error(f"{box.alias} has no instance_id in {boxes_mod.REGISTRY_PATH}")
        return 2

    client = aws_env.ec2(region)
    resp = client.describe_instances(InstanceIds=[box.instance_id])
    reservations = resp.get("Reservations") or []
    if not reservations:
        logger.error(
            f"instance {box.instance_id} for alias {box.alias!r} does not exist "
            f"in account {account} (region={region}). It was probably "
            f"terminated.\n  Remove the entry from {boxes_mod.REGISTRY_PATH} and "
            f"re-run to launch a new box."
        )
        return 2
    instance = reservations[0]["Instances"][0]
    state = instance["State"]["Name"]
    host = box.ssh_host

    # `stopping` can't be started — AWS rejects it. Ride out the transition.
    if state == "stopping":
        print(f"instance {box.instance_id} is stopping; waiting for it to settle…")
        client.get_waiter("instance_stopped").wait(InstanceIds=[box.instance_id])
        state = "stopped"

    # Resizing is the whole reason to re-run spinup on some boxes, and AWS only
    # accepts a type change while the instance is stopped. Doing it here keeps
    # the root volume — and the browser login on it — that teardown would
    # destroy. Without this the chosen type would be silently ignored on every
    # already-registered alias.
    current_type = instance.get("InstanceType")
    if args.instance_type and args.instance_type != current_type:
        print(f"\ninstance type: {current_type} → {args.instance_type}")
        if state == "running":
            if not args.yes:
                # The idle check further down only runs after the restart, by
                # which point a task would already have been killed. Ask the
                # live box first.
                print(f"  Checking worker idle state on {host}…")
                if not _remote_is_idle(host, cfg.key_name):
                    logger.error(
                        "Refusing to resize: worker is not strictly idle (has "
                        "a current task and/or a non-empty queue). Stopping it "
                        "now would lose that work.\n"
                        f"  Inspect with: dispatch show {box.alias}\n"
                        f"  Resize anyway with -y."
                    )
                    return 1
                print("  Resizing stops the instance first; the root volume "
                      "and its browser login survive.")
                answer = input("  Stop, resize and start again? [y/N]: ")
                if answer.strip().lower() not in {"y", "yes"}:
                    print("Left at the current size; nothing changed.")
                    return 1
            print(f"  stopping {box.instance_id}…")
            client.stop_instances(InstanceIds=[box.instance_id])
            client.get_waiter("instance_stopped").wait(
                InstanceIds=[box.instance_id])
            state = "stopped"
        client.modify_instance_attribute(
            InstanceId=box.instance_id,
            InstanceType={"Value": args.instance_type},
        )
        print(f"  resized to {args.instance_type}")

    if state == "stopped":
        print(f"instance {box.instance_id} is stopped — starting it "
              f"(not launching a new one).")
        client.start_instances(InstanceIds=[box.instance_id])
        client.get_waiter("instance_running").wait(InstanceIds=[box.instance_id])
        # The public DNS is reassigned on every start, so the host cached
        # before the stop now points at nothing — or eventually at somebody
        # else's instance. Re-read it before anything tries to connect.
        instance = client.describe_instances(InstanceIds=[box.instance_id])[
            "Reservations"][0]["Instances"][0]
        host = instance.get("PublicDnsName") or ""
        if not host:
            logger.error(f"instance {box.instance_id} started but has no public DNS")
            return 1
        print(f"  new public DNS: {host}")
        boxes_mod.set_field(box.alias, "ssh_host", host)
        state = "running"
        wait_for(f"SSH on {host}",
                 lambda: ssh_run(host, cfg.key_name, "true",
                                 check=False, timeout=15).returncode == 0)
    elif state != "running":
        logger.error(
            f"instance {box.instance_id} for alias {box.alias!r} is {state} — "
            f"cannot re-provision.\n  Wait for it to settle, or run "
            f"`dispatch teardown` and re-run to launch fresh."
        )
        return 2

    boxes_mod.set_field(box.alias, "status", boxes_mod.STATUS_RUNNING)
    # Write the type we just observed (or just applied), not only on a resize:
    # EC2 is the authority and this is a cache, so every re-run is a chance to
    # correct an entry that predates the field or was resized elsewhere.
    effective_type = args.instance_type or current_type
    if effective_type and effective_type != box.instance_type:
        boxes_mod.set_field(box.alias, "instance_type", effective_type)

    if not args.yes:
        print(f"Checking worker idle state on {host}…")
        if not _remote_is_idle(host, cfg.key_name):
            logger.error(
                "Refusing to re-run: worker is not strictly idle (has a "
                "current task and/or a non-empty queue).\n"
                f"  Inspect with: dispatch show {box.alias}"
            )
            return 1

    push_setup(host, cfg=cfg, template=template, db_url=db_url, restart=True)
    print("\n" + "=" * 56)
    print("Re-pushed setup to existing box.")
    print(f"  alias:       {box.alias}")
    print(f"  instance_id: {box.instance_id}")
    print(f"  public_dns:  {host}")
    print("=" * 56)
    return 0


def _launch(args, cfg, account, sg_id, template, db_url, info, region) -> int:
    """Launch a brand-new instance and provision it."""
    ami = args.ami or cfg.ami
    ami_source = "--ami" if args.ami else f"{aws_env.REPO_CONFIG} aws.gui_ami"
    if not ami:
        logger.error(
            f"no AMI: aws.gui_ami is unset in {aws_env.REPO_CONFIG} and no "
            f"--ami was given.\n  Find the current Ubuntu 22.04 id with:\n"
            f"    aws ec2 describe-images --region {region} --owners 099720109477 \\\n"
            f"      --filters 'Name=name,Values=ubuntu/images/hvm-ssd/"
            f"ubuntu-jammy-22.04-amd64-server-*' \\\n"
            f"      --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name]' "
            f"--output text"
        )
        return 2
    # AMI ids are region-specific, so a config value carried to another region
    # names nothing. Checking here turns an opaque InvalidAMIID.NotFound from
    # run_instances into a message that says which id and which region.
    ami_name = aws_env.describe_image(ami, region)
    if not ami_name:
        logger.error(
            f"AMI {ami!r} does not exist in region {region} (from {ami_source}).\n"
            f"  AMI ids are region-specific. Update aws.gui_ami in "
            f"{aws_env.REPO_CONFIG} for this region, or pass --ami."
        )
        return 2
    print(f"\nAMI: {ami}  ({ami_name})")
    print(f"     from {ami_source}")

    print(f"Launching alias={args.alias} provider={info.provider} "
          f"type={args.instance_type} region={region}")
    client = aws_env.ec2(region)
    resp = client.run_instances(
        ImageId=ami,
        InstanceType=args.instance_type,
        KeyName=cfg.key_name,
        SecurityGroupIds=[sg_id],
        MinCount=1,
        MaxCount=1,
        BlockDeviceMappings=[{
            "DeviceName": "/dev/sda1",
            "Ebs": {
                "VolumeSize": int(args.volume_size),
                "VolumeType": "gp3",
                "DeleteOnTermination": True,
            },
        }],
        UserData=USER_DATA,
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Project", "Value": "gui-agents"},
                {"Key": "alias", "Value": args.alias},
                {"Key": "provider", "Value": info.provider},
                {"Key": "Name", "Value": f"gui-agents-{args.alias}"},
            ],
        }],
    )
    instance_id = resp["Instances"][0]["InstanceId"]
    print(f"InstanceId: {instance_id}")
    print("Waiting for instance to reach 'running'…")
    client.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    host = client.describe_instances(InstanceIds=[instance_id])[
        "Reservations"][0]["Instances"][0].get("PublicDnsName") or ""
    if not host:
        logger.error(f"instance {instance_id} has no public DNS name")
        return 1

    # Register BEFORE provisioning, so a failure partway through still leaves
    # the box recoverable by re-running against the same alias.
    boxes_mod.add_box(
        alias=args.alias,
        instance_id=instance_id,
        instance_type=args.instance_type,
        ssh_host=host,
        ssh_key=f"~/.ssh/{cfg.key_name}.pem",
        status=boxes_mod.STATUS_RUNNING,
    )

    wait_for(f"SSH on {host}",
             lambda: ssh_run(host, cfg.key_name, "true",
                             check=False, timeout=15).returncode == 0)
    wait_for("cloud-init to finish (/var/lib/gui-agents/.bootstrap-done)",
             lambda: ssh_run(host, cfg.key_name,
                             "test -f /var/lib/gui-agents/.bootstrap-done",
                             check=False, timeout=15).returncode == 0)

    push_setup(host, cfg=cfg, template=template, db_url=db_url, restart=False)

    print("\n" + "=" * 56)
    print("Instance launched and worker active.")
    print(f"  alias:       {args.alias}")
    print(f"  instance_id: {instance_id}")
    print(f"  public_dns:  {host}")
    print(f"\nSSH:\n    ssh -i {_key_path(cfg.key_name)} ubuntu@{host}")
    print("\nLog in to the browser, then verify:")
    print(f"    dispatch login {args.alias}")
    print("    dispatch status")
    print("=" * 56)
    return 0


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def _select_boxes(args) -> list | None:
    """Resolve --alias / --all against the registry."""
    try:
        registry = boxes_mod.load_boxes()
    except FileNotFoundError as e:
        logger.error(str(e))
        return None
    if not registry:
        logger.error("no boxes in registry")
        return None
    if args.all:
        return registry
    matches = [b for b in registry if b.alias == args.alias]
    if not matches:
        known = ", ".join(b.alias for b in registry) or "(none)"
        logger.error(f"unknown alias {args.alias!r}. Known: {known}")
        return None
    return matches


def cmd_stop(args) -> int:
    """Stop instances without destroying them."""
    region = args.region
    try:
        cfg, account = aws_env.connect(region)
    except FleetConfigError as e:
        logger.error(str(e))
        return 2

    selected = _select_boxes(args)
    if selected is None:
        return 2

    client = aws_env.ec2(region)
    targets: list[tuple[str, str]] = []
    for box in selected:
        if not box.instance_id:
            logger.warning(f"{box.alias}: no instance_id — skipping")
            continue
        resp = client.describe_instances(
            InstanceIds=[box.instance_id]) if box.instance_id else {}
        reservations = resp.get("Reservations") or []
        if not reservations:
            logger.warning(
                f"{box.alias} ({box.instance_id}): not found in account "
                f"{account} — skipping"
            )
            continue
        state = reservations[0]["Instances"][0]["State"]["Name"]
        if state in ("stopped", "stopping"):
            print(f"  {box.alias} ({box.instance_id}): already {state} — skipping")
            boxes_mod.set_field(box.alias, "status", boxes_mod.STATUS_STOPPED)
            continue
        if state != "running":
            logger.warning(f"{box.alias}: state={state}, cannot stop — skipping")
            continue

        # A stopped instance loses the running agent outright: the task is
        # not written to task_attempts and not returned to any queue, so the
        # work is simply gone.
        if not args.force and box.ssh_host and cfg.key_name:
            task = _current_task(box.ssh_host, cfg.key_name)
            if task:
                logger.error(
                    f"{box.alias}: BUSY — task {task} is running; stopping "
                    f"would lose it. Wait for it, cancel it, or pass --force."
                )
                continue
        targets.append((box.alias, box.instance_id))

    if not targets:
        print("\nnothing to stop.")
        return 0

    print(f"\nWill STOP {len(targets)} instance(s) — volumes and browser "
          f"logins are kept:")
    for alias, iid in targets:
        print(f"  {alias:<14} {iid}")
    if not args.yes:
        try:
            if input("\nProceed? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print("aborted")
                return 0
        except EOFError:
            print("aborted")
            return 0

    ids = [iid for _, iid in targets]
    client.stop_instances(InstanceIds=ids)
    print(f"Stop requested for {len(ids)} instance(s). Waiting…")
    client.get_waiter("instance_stopped").wait(InstanceIds=ids)

    for alias, _ in targets:
        boxes_mod.set_field(alias, "status", boxes_mod.STATUS_STOPPED)
        # Blank the cached host: a stopped instance has no public DNS, and the
        # one it gets on restart will be different.
        boxes_mod.set_field(alias, "ssh_host", "")

    print(f"Done. {len(ids)} instance(s) stopped; {boxes_mod.REGISTRY_PATH} updated.")
    print("\nCompute billing stops now; the root volume still bills as storage.")
    print(f"Restart with: dispatch spinup --alias {targets[0][0]} "
          f"--config-template <file>")
    print("To stop storage costs too: dispatch teardown --alias <name>")
    return 0


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


def cmd_teardown(args) -> int:
    """Terminate instances. Destroys the root volume and the browser login."""
    region = args.region
    try:
        cfg, account = aws_env.connect(region)
    except FleetConfigError as e:
        logger.error(str(e))
        return 2

    client = aws_env.ec2(region)
    # Selected by TAG, not by the registry: terminating a box the registry
    # lost track of is exactly the cleanup you want. (`stop` goes by registry
    # instead, since stopping an unregistered box would strand an instance
    # nothing can restart by alias.)
    filters = [
        {"Name": "tag:Project", "Values": ["gui-agents"]},
        {"Name": "instance-state-name",
         "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    if not args.all:
        filters.append({"Name": "tag:alias", "Values": [args.alias]})

    reservations = client.describe_instances(Filters=filters).get("Reservations") or []
    found = [i for r in reservations for i in r["Instances"]]
    if not found:
        print(f"no matching instances found in account {account} (region={region})")
        return 0

    print(f"\nFound {len(found)} instance(s) in account {account}:")
    rows = []
    for inst in found:
        alias = next((t["Value"] for t in inst.get("Tags", [])
                      if t["Key"] == "alias"), "-")
        rows.append((alias, inst["InstanceId"], inst["State"]["Name"]))
        print(f"  {alias:<14} {inst['InstanceId']:<20} {inst['State']['Name']}")
    print("\nTerminating DESTROYS the root volume — the Chrome profile and its")
    print("logged-in cookies go with it. Use `dispatch stop` to halt a box and")
    print("keep them.")

    if not args.yes:
        try:
            if input("\nTerminate these? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print("aborted")
                return 0
        except EOFError:
            print("aborted")
            return 0

    ids = [iid for _, iid, _ in rows]
    client.terminate_instances(InstanceIds=ids)
    print(f"Termination requested for {len(ids)} instance(s). Waiting…")
    client.get_waiter("instance_terminated").wait(InstanceIds=ids)
    print("Done.")

    removed = boxes_mod.remove_boxes([alias for alias, _, _ in rows if alias != "-"])
    if removed:
        print(f"Pruned {removed} entr{'y' if removed == 1 else 'ies'} from "
              f"{boxes_mod.REGISTRY_PATH}")
    return 0
