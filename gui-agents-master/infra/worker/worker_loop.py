"""`gui-agents-worker` — the long-running loop that pops tasks from
state.json and executes them one at a time.

Runs as a systemd service on each EC2 box. Each task is launched inside a
transient unit via `systemd-run --unit=gui-agents-task-<id> --wait` so it
can be cancelled (`systemctl stop gui-agents-task-<id>`) or inspected
(`journalctl -u gui-agents-task-<id>`) independently of this loop.

Config changes are applied by `gui-agents-queue config push`, which
replaces `infra/configs/configs.yaml` and restarts this service — so
between restarts we treat cfg as stable.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.configs import load_configs, resolve_agent_identity  # noqa: E402
from infra.worker import state as S  # noqa: E402
from infra.worker.auth_probe import read_auth  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("gui-agents-worker")

IDLE_SLEEP_SEC = 5
AUTH_BLOCKED_LOG_EVERY_SEC = 60

_shutdown = False
_last_auth_blocked_log_at: float = 0.0


def _handle_sigterm(signum, frame):
    global _shutdown
    logger.info(f"Received signal {signum}; will exit after current iteration.")
    _shutdown = True


def _worker_id() -> str:
    """Prefer EC2 instance id; fall back to hostname for local dev."""
    try:
        with open("/var/lib/cloud/data/instance-id") as f:
            return f.read().strip()
    except OSError:
        pass
    try:
        import urllib.request

        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        return urllib.request.urlopen(req, timeout=0.5).read().decode()
    except Exception:
        return socket.gethostname()


def _publish_worker_info() -> None:
    """Read configs.yaml, capture the summary into state.worker."""
    try:
        cfg = load_configs()
    except Exception as e:
        logger.error(f"load_configs failed; worker info not updated: {e}")
        return

    provider = getattr(cfg.provider, "kind", "") or ""
    agent = getattr(cfg, "agent", None)
    try:
        identity = resolve_agent_identity(cfg)
        agent_model_name = identity.model_name
    except Exception as e:
        logger.error(
            f"resolve_agent_identity failed; agent_model_name will be empty: {e}"
        )
        agent_model_name = ""
    info = S.WorkerInfo(
        worker_id=_worker_id(),
        hostname=socket.gethostname(),
        provider=provider,
        agent_model_name=agent_model_name,
        prompt_version=getattr(agent, "prompt_version", None) if agent else None,
    )
    S.set_worker_info(info)
    logger.info(
        f"worker info: id={info.worker_id} provider={info.provider} "
        f"agent_model={info.agent_model_name} prompt_version={info.prompt_version}"
    )


def _systemctl_is_active(unit: str) -> str:
    """Returns 'active' | 'inactive' | 'failed' | 'unknown' etc."""
    if shutil.which("systemctl") is None:
        return "unknown"
    proc = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
    )
    return (proc.stdout or "").strip() or (proc.stderr or "").strip()


def _reconcile_stale_current() -> None:
    """If state.current names a unit that is not active, the runner must
    have died outside our control. Mark the task 'crashed' and move on."""
    snap = S.read_state()
    if snap.current is None:
        return
    unit = snap.current.unit
    status = _systemctl_is_active(unit)
    if status in ("active", "activating", "unknown"):
        return
    logger.warning(
        f"stale current: unit={unit} systemctl={status}; "
        f"marking task_id={snap.current.task_id} crashed"
    )
    S.finish_current(status="crashed")


def _run_task_unit(task_id: str, unit: str) -> int:
    """Launch `python -m infra.run --task-id <id>` inside a transient unit.
    Falls back to direct subprocess when systemd-run is unavailable (local
    dev / macOS). Returns the child's exit code."""
    run_cmd = [
        sys.executable,
        "-m",
        "infra.run",
        "--task-id",
        str(task_id),
        "--yes",
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(_REPO_ROOT))

    if shutil.which("systemd-run") is not None:
        # Transient units get a fresh environment — the worker's own
        # EnvironmentFile is NOT inherited. Load it explicitly so infra.run
        # sees MBABENCHV2JUDGE_KEYS_DATABASE_URL / AWS_* inside the unit.
        cmd = [
            "systemd-run",
            f"--unit={unit}",
            "--wait",
            "--collect",
            f"--working-directory={_REPO_ROOT}",
            "--property=EnvironmentFile=/etc/gui-agents/secrets.env",
            # Chrome is owned by gui-agents-chrome.service — tasks must not
            # spawn their own. Propagate the flag into the transient unit.
            "--property=Environment=GUI_AGENTS_CHROME_MANAGED=1",
            "--pipe",
        ] + run_cmd
        logger.info(f"launch: {' '.join(cmd)}")
        proc = subprocess.run(cmd, env=env)
        return proc.returncode

    logger.warning(
        "systemd-run not found; running task inline (local dev mode). "
        "Cancel with Ctrl-C on the worker; no per-task unit will exist."
    )
    proc = subprocess.run(run_cmd, env=env, cwd=str(_REPO_ROOT))
    return proc.returncode


def _status_from_returncode(rc: int) -> str:
    if rc == 0:
        return "success"
    if rc == 2:
        return "config_error"
    return "failed"


def _run_one_if_queued() -> bool:
    """If idle and the queue is non-empty, pop one and run it. Returns True
    if a task was run (caller should skip the idle sleep)."""
    snap = S.read_state()
    if snap.current is not None or not snap.queue:
        return False

    # Don't start new tasks while the browser session is known-bad.
    # Task stays at the head of the queue; the auth-probe timer will flip
    # auth.json back to ok after the operator re-logs in via VNC.
    auth = read_auth()
    if not auth or not auth.get("ok"):
        global _last_auth_blocked_log_at
        now = time.monotonic()
        if now - _last_auth_blocked_log_at >= AUTH_BLOCKED_LOG_EVERY_SEC:
            reason = (auth or {}).get("reason") or "no_probe_yet"
            logger.warning(
                f"not starting task: auth gate blocked (reason={reason}); "
                f"{len(snap.queue)} task(s) still queued"
            )
            _last_auth_blocked_log_at = now
        return False

    next_task_id = snap.queue[0].task_id
    unit = f"gui-agents-task-{next_task_id}"
    current = S.pop_head_as_current(unit)
    if current is None:
        return False

    logger.info(f"starting task_id={current.task_id} unit={unit}")
    rc = _run_task_unit(current.task_id, unit)
    status = _status_from_returncode(rc)
    S.finish_current(status=status)
    logger.info(f"finished task_id={current.task_id} rc={rc} status={status}")
    return True


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    _publish_worker_info()
    logger.info("worker loop started")

    while not _shutdown:
        try:
            _reconcile_stale_current()
            ran = _run_one_if_queued()
            if _shutdown:
                break
            if not ran:
                time.sleep(IDLE_SLEEP_SEC)
        except Exception:
            logger.exception("worker loop iteration crashed; sleeping 30s")
            time.sleep(30)

    logger.info("worker loop exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
