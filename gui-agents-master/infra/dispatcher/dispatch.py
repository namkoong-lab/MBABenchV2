"""Laptop-side dispatcher CLI.

Fans out over SSH to read box state and push task assignments. No central
daemon, no DB-backed message queue — each invocation reads/writes box state
over SSH and exits.

Commands:
    dispatch status [--follow]
    dispatch show <alias>
    dispatch assign --n N [--agent X] [--task-source Y]
    dispatch assign --tasks 42,43,44 [--box <alias>]
    dispatch cancel <alias> <task_id>
    dispatch clear <alias>
    dispatch logs <alias> [--task <id>] [-f]
    dispatch login <alias> [--local-port N] [--no-open]
    dispatch config pull <alias>
    dispatch config push <alias> <localfile>
    dispatch config diff <aliasA> <aliasB>
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import difflib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.configs import load_configs  # noqa: E402
from infra.dispatcher.boxes import Box, find_by_alias, load_boxes  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("dispatch")

SSH_TIMEOUT_SEC = 15


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def _ssh_cmd(box: Box, remote_cmd: str) -> list[str]:
    return ["ssh", *box.ssh_base_args(), box.ssh_target(), remote_cmd]


@dataclass
class SSHResult:
    returncode: int
    stdout: str
    stderr: str


def _ssh_exec(box: Box, remote_cmd: str, stdin: str | None = None, timeout: int = SSH_TIMEOUT_SEC) -> SSHResult:
    proc = subprocess.run(
        _ssh_cmd(box, remote_cmd),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return SSHResult(proc.returncode, proc.stdout, proc.stderr)


def _fetch_state(box: Box) -> dict | None:
    """Run `gui-agents-queue show` on a box and parse the JSON. Returns
    None if SSH fails or the output isn't parseable."""
    try:
        r = _ssh_exec(box, "gui-agents-queue show")
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _fetch_all_states(boxes: list[Box]) -> dict[str, dict | None]:
    """Parallel fan-out: {alias: state_dict_or_None}."""
    out: dict[str, dict | None] = {}
    with cf.ThreadPoolExecutor(max_workers=max(4, len(boxes))) as ex:
        futures = {ex.submit(_fetch_state, b): b.alias for b in boxes}
        for fut in cf.as_completed(futures):
            out[futures[fut]] = fut.result()
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _fmt_duration(start_iso: str | None) -> str:
    if not start_iso:
        return ""
    try:
        started = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - started
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


_AUTH_STALE_AFTER_SEC = 30 * 60  # checked_at older than this → "?"


def _fmt_auth(auth: dict | None) -> str:
    """Render the auth-probe column. Values:
      ok       — last probe succeeded and is fresh
      STALE    — last probe failed (any reason) — needs re-login
      old      — last probe succeeded but was too long ago
      ?        — no probe result on this box
    """
    if not auth:
        return "?"
    checked_at = auth.get("checked_at") or ""
    age_s = None
    try:
        when = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        age_s = (datetime.now(timezone.utc) - when).total_seconds()
    except ValueError:
        pass
    if not auth.get("ok"):
        return "STALE"
    if age_s is not None and age_s > _AUTH_STALE_AFTER_SEC:
        return "old"
    return "ok"


def _fmt_row(alias: str, state: dict | None) -> str:
    if state is None:
        return f"  {alias:<18} UNREACHABLE"
    worker = state.get("worker") or {}
    current = state.get("current")
    queue = state.get("queue") or []
    completed = state.get("completed") or []
    auth = state.get("auth")

    wid = (worker.get("worker_id") or "-")[:18]
    agent = worker.get("agent_model_name") or "-"
    pv = worker.get("prompt_version")
    pv_s = f"(v{pv})" if pv is not None else ""
    if current:
        cur_s = f"task={current.get('task_id')} ({_fmt_duration(current.get('started_at'))})"
    else:
        cur_s = "idle"
    last = completed[-1] if completed else None
    last_s = f"{last.get('task_id')} {last.get('status')}" if last else "-"
    login_s = _fmt_auth(auth)
    return (
        f"  {alias:<12} {wid:<20} {agent:<14} {pv_s:<5} "
        f"{login_s:<6} {cur_s:<24} +{len(queue):<3} {last_s}"
    )


def _print_status_table(boxes: list[Box], states: dict[str, dict | None]) -> None:
    header = (
        f"  {'alias':<12} {'worker_id':<20} {'agent':<14} {'pv':<5} "
        f"{'login':<6} {'current':<24} {'q':<4} last"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for b in boxes:
        print(_fmt_row(b.alias, states.get(b.alias)))


# ---------------------------------------------------------------------------
# DB access for `assign`
# ---------------------------------------------------------------------------


def _get_db_url() -> str:
    cfg = load_configs()
    db_cfg = getattr(cfg, "database", None)
    direct = getattr(db_cfg, "url", "") or ""
    if direct:
        return direct
    env_name = getattr(db_cfg, "url_env", "") or ""
    if env_name:
        v = os.environ.get(env_name, "") or ""
        if v:
            return v
    raise ValueError(
        "Could not resolve database URL. Set database.url in "
        "infra/configs/configs.yaml or export the env var named in "
        "database.url_env."
    )


def _list_eligible_tasks(
    *,
    agent_model_name: str,
    prompt_version: int | str | None,
    task_sources: list[str] | None,
    limit: int,
    skip_deprecated: bool = True,
    skip_already_attempted: bool = True,
) -> list[dict]:
    """Query the Bizbench `tasks` table for eligible rows. Mirrors the
    filters used by BizbenchPostgresS3TaskSource, minus file download."""
    import psycopg2
    from psycopg2 import sql
    import psycopg2.extras

    where = ["TRUE"]
    params: dict = {"limit": limit}
    if skip_deprecated:
        where.append("t.deprecated = FALSE")
    if task_sources:
        where.append("t.task_source = ANY(%(task_sources)s)")
        params["task_sources"] = list(task_sources)
    if skip_already_attempted:
        where.append(
            "NOT EXISTS ("
            "  SELECT 1 FROM task_attempts ta"
            "  WHERE ta.task_id = t.id"
            "    AND ta.agent_model_name = %(agent_model_name)s"
            "    AND ta.prompt_version   = %(prompt_version)s"
            "    AND ta.agent_failed     = FALSE"
            "    AND ta.deprecated       = FALSE"
            ")"
        )
        params["agent_model_name"] = agent_model_name
        params["prompt_version"] = prompt_version

    q = sql.SQL(
        "SELECT t.id, t.task_name, t.task_source "
        "FROM tasks t "
        "WHERE {where} "
        "ORDER BY t.id "
        "LIMIT %(limit)s"
    ).format(where=sql.SQL(" AND ").join(sql.SQL(w) for w in where))

    conn = psycopg2.connect(_get_db_url())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, params)
            return list(cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    boxes = load_boxes()
    if not boxes:
        logger.error("no boxes in registry")
        return 2
    if args.follow:
        try:
            while True:
                states = _fetch_all_states(boxes)
                print("\033[2J\033[H", end="")  # clear screen
                print(f"dispatch status — {datetime.now().isoformat(timespec='seconds')}\n")
                _print_status_table(boxes, states)
                time.sleep(5)
        except KeyboardInterrupt:
            return 0
    else:
        states = _fetch_all_states(boxes)
        _print_status_table(boxes, states)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    box = find_by_alias(args.alias)
    state = _fetch_state(box)
    if state is None:
        logger.error(f"{box.alias} UNREACHABLE")
        return 2
    print(json.dumps(state, indent=2))
    return 0


def _distribute_least_loaded(
    tasks: list[dict],
    boxes: list[Box],
    states: dict[str, dict | None],
) -> list[tuple[Box, dict]]:
    """Least-loaded-first distribution. Returns (box, task) pairs."""
    eligible: list[tuple[int, Box]] = []
    for b in boxes:
        st = states.get(b.alias)
        if st is None:
            continue
        load = len(st.get("queue") or []) + (1 if st.get("current") else 0)
        eligible.append((load, b))
    if not eligible:
        return []
    assignments: list[tuple[Box, dict]] = []
    for task in tasks:
        eligible.sort(key=lambda pair: pair[0])
        load, b = eligible[0]
        assignments.append((b, task))
        eligible[0] = (load + 1, b)
    return assignments


def _group_boxes_by_agent(
    boxes: list[Box], states: dict[str, dict | None]
) -> dict[tuple[str, str], list[Box]]:
    """Group reachable boxes by (agent_model_name, str(prompt_version))."""
    out: dict[tuple[str, str], list[Box]] = {}
    for b in boxes:
        st = states.get(b.alias)
        if st is None:
            continue
        worker = st.get("worker") or {}
        agent = worker.get("agent_model_name") or ""
        pv = str(worker.get("prompt_version"))
        if not agent:
            continue
        out.setdefault((agent, pv), []).append(b)
    return out


def cmd_assign(args: argparse.Namespace) -> int:
    boxes = load_boxes()
    if args.box:
        boxes = [find_by_alias(args.box)]

    states = _fetch_all_states(boxes)
    reachable = [b for b in boxes if states.get(b.alias) is not None]
    if not reachable:
        logger.error("no reachable boxes")
        return 2

    # Explicit task IDs — resolve names by DB lookup so the box gets a
    # readable label; fall back to empty name on missing row.
    if args.tasks:
        task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
        tasks = [{"id": tid, "task_name": None, "task_source": None} for tid in task_ids]
        assignments: list[tuple[Box, dict]] = []
        if args.box:
            for t in tasks:
                assignments.append((reachable[0], t))
        else:
            assignments = _distribute_least_loaded(tasks, reachable, states)
    else:
        if args.n is None:
            logger.error("must pass either --tasks or --n")
            return 2
        groups = _group_boxes_by_agent(reachable, states)
        if args.agent:
            groups = {k: v for k, v in groups.items() if k[0] == args.agent}
        if not groups:
            logger.error("no reachable boxes match the agent filter")
            return 2
        assignments = []
        for (agent, pv), group_boxes in groups.items():
            pv_val: int | str | None
            if pv == "None":
                pv_val = None
            else:
                try:
                    pv_val = int(pv)
                except ValueError:
                    pv_val = pv
            tasks = _list_eligible_tasks(
                agent_model_name=agent,
                prompt_version=pv_val,
                task_sources=([args.task_source] if args.task_source else None),
                limit=args.n,
            )
            logger.info(
                f"group agent={agent} pv={pv}: {len(tasks)} eligible task(s), "
                f"{len(group_boxes)} box(es)"
            )
            assignments += _distribute_least_loaded(tasks, group_boxes, states)

    if not assignments:
        logger.info("nothing to assign")
        return 0

    print(f"\nAssigning {len(assignments)} task(s):")
    for box, task in assignments:
        print(f"  -> {box.alias:<12} task={task.get('id')} name={task.get('task_name') or ''}")
    if not args.yes:
        try:
            resp = input("\nProceed? [y/N]: ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in {"y", "yes"}:
            logger.info("aborted")
            return 0

    failures = 0
    for box, task in assignments:
        tid = str(task.get("id"))
        name = (task.get("task_name") or "").replace("'", "")
        remote = f"gui-agents-queue add {tid}" + (f" '{name}'" if name else "")
        r = _ssh_exec(box, remote)
        if r.returncode != 0:
            failures += 1
            logger.error(f"{box.alias} add {tid} failed: {r.stderr.strip()}")
        else:
            logger.info(f"{box.alias} add {tid} ok")
    return 1 if failures else 0


def cmd_cancel(args: argparse.Namespace) -> int:
    box = find_by_alias(args.alias)
    state = _fetch_state(box)
    if state is None:
        logger.error(f"{box.alias} UNREACHABLE")
        return 2
    tid = args.task_id
    current = state.get("current") or {}
    if str(current.get("task_id")) == str(tid):
        unit = current.get("unit") or f"gui-agents-task-{tid}"
        r = _ssh_exec(box, f"systemctl stop {unit}")
        if r.returncode != 0:
            logger.error(f"stop {unit}: {r.stderr.strip()}")
            return r.returncode
        print(f"stopped {unit} on {box.alias}")
        return 0
    queued = [q for q in state.get("queue") or [] if str(q.get("task_id")) == str(tid)]
    if queued:
        r = _ssh_exec(box, f"gui-agents-queue remove {tid}")
        if r.returncode != 0:
            logger.error(f"remove {tid}: {r.stderr.strip()}")
            return r.returncode
        print(f"removed queued task_id={tid} from {box.alias}")
        return 0
    logger.error(f"task_id={tid} is neither current nor queued on {box.alias}")
    return 1


def cmd_clear(args: argparse.Namespace) -> int:
    box = find_by_alias(args.alias)
    r = _ssh_exec(box, "gui-agents-queue clear")
    if r.returncode != 0:
        logger.error(r.stderr.strip())
        return r.returncode
    print(r.stdout.strip())
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    box = find_by_alias(args.alias)
    unit = f"gui-agents-task-{args.task}" if args.task else "gui-agents-worker.service"
    parts = ["journalctl", "-u", unit]
    if args.follow:
        parts.append("-f")
    # +G opens the pager at the bottom so the most recent lines are visible first.
    remote = "SYSTEMD_LESS='FRXMK +G' " + " ".join(parts)
    # interactive streaming — don't capture.
    return subprocess.call(["ssh", "-t", *box.ssh_base_args(), box.ssh_target(), remote])


def cmd_login(args: argparse.Namespace) -> int:
    """Open a VNC tunnel to a box so the operator can log in to
    claude.ai / chatgpt.com.

    Xvfb runs on :99 persistently but the worker only spawns Chrome
    during a task — when idle the display is blank. So this command
    also launches Chrome on :99 against the worker's --user-data-dir
    (read from the box's configs.yaml), then tears it down on exit.
    Cookies persist under that profile dir, so the worker picks up the
    refreshed session on its next task.
    """
    box = find_by_alias(args.alias)
    state = _fetch_state(box)
    if state is None:
        logger.error(f"{box.alias} UNREACHABLE — cannot verify idle state")
        return 2
    current = state.get("current")
    if current:
        logger.error(
            f"{box.alias} is busy (task={current.get('task_id')}); "
            f"login would collide with worker Chrome. "
            f"Cancel the task first or wait for it to finish."
        )
        return 2
    local_port = args.local_port
    remote_port = 5901
    # macOS Screen Sharing refuses passwordless VNC even on localhost, so
    # mint a fresh random password per session. The tunnel is already
    # SSH-protected and -localhost binds x11vnc to 127.0.0.1 on the box.
    import secrets
    vnc_pw = secrets.token_urlsafe(9)[:8]
    # BatchMode=yes (from ssh_base_args) silences password prompts but is
    # also fine here since we require key auth. -t forces a pty so x11vnc
    # dies with SIGHUP when the user Ctrl-Cs or closes the session.
    remote_cmd = f"""\
set -u
cd /opt/gui-agents-master
EVAL_OUT=$(PYTHONPATH=. python3 -c '
import os
from infra.configs import load_configs
c = load_configs()
prov = c.provider.kind
block = c.claude_web if prov == "claude" else c.chatgpt_web
pd = os.path.expanduser(block.browser.profile_dir)
candidates = ["/usr/bin/google-chrome-canary", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome-unstable"]
chrome = next((p for p in candidates if os.path.exists(p)), "")
url = "https://chatgpt.com/" if prov == "chatgpt" else "https://claude.ai/"
print(f"PROVIDER={{prov}}")
print(f"PROFILE_DIR={{pd}}")
print(f"CHROME_BIN={{chrome}}")
print(f"LOGIN_URL={{url}}")
') || {{ echo "ERROR: failed to read box config" >&2; exit 1; }}
eval "$EVAL_OUT"
if [ -z "${{CHROME_BIN:-}}" ]; then echo "ERROR: chrome not found on box" >&2; exit 1; fi
echo "INFO: provider=$PROVIDER profile=$PROFILE_DIR chrome=$CHROME_BIN" >&2

if ! sudo -n true 2>/dev/null; then
  echo "ERROR: passwordless sudo required (worker runs as root, profile dir is root-owned)" >&2
  exit 1
fi

sudo -n pkill -x chrome >/dev/null 2>&1 || true
sudo -n pkill -x google-chrome >/dev/null 2>&1 || true
pkill -x x11vnc >/dev/null 2>&1 || true
sleep 0.3

sudo -n mkdir -p "$PROFILE_DIR"
sudo -n env DISPLAY=:99 "$CHROME_BIN" \\
  --user-data-dir="$PROFILE_DIR" \\
  --no-first-run --no-default-browser-check --no-sandbox \\
  --remote-debugging-port=9222 \\
  "$LOGIN_URL" >/dev/null 2>&1 &
CHROME_PID=$!
sleep 1
if ! sudo -n pgrep -x chrome >/dev/null 2>&1 && ! sudo -n pgrep -x google-chrome >/dev/null 2>&1; then
  echo "ERROR: chrome failed to start on :99 (check journalctl or run chrome manually)" >&2
  exit 1
fi

cleanup() {{
  pkill -x x11vnc >/dev/null 2>&1 || true
}}
trap cleanup EXIT INT TERM HUP

x11vnc -display :99 -localhost -passwd '{vnc_pw}' -rfbport {remote_port} -forever -quiet
"""
    ssh_args = [
        "ssh", "-t",
        "-L", f"{local_port}:localhost:{remote_port}",
        *box.ssh_base_args(),
        box.ssh_target(),
        remote_cmd,
    ]
    logger.info(
        f"{box.alias}: x11vnc starting on :99, forwarded to localhost:{local_port}"
    )
    logger.info(
        f"connect a VNC viewer to vnc://localhost:{local_port} — "
        f"log in to claude.ai / chatgpt.com in the Chrome window you see, "
        f"then Ctrl-C here to tear down."
    )
    logger.info(f"VNC password (one-shot): {vnc_pw}")
    if sys.platform == "darwin" and not args.no_open:
        # Small delay so x11vnc has a chance to bind before Screen Sharing
        # connects. Running as a thread so we don't block the ssh call.
        import threading

        def _open_viewer() -> None:
            time.sleep(2)
            subprocess.run(
                ["open", f"vnc://localhost:{local_port}"], check=False
            )

        threading.Thread(target=_open_viewer, daemon=True).start()
    return subprocess.call(ssh_args)


def cmd_config_pull(args: argparse.Namespace) -> int:
    box = find_by_alias(args.alias)
    r = _ssh_exec(box, "cat /var/lib/gui-agents/configs.yaml")
    if r.returncode != 0:
        # Fallback: try the in-repo path the queue CLI writes to.
        r = _ssh_exec(
            box,
            "cat \"$(python3 -c 'from infra.worker.queue_cli import CONFIGS_YAML; print(CONFIGS_YAML)')\"",
        )
    if r.returncode != 0:
        logger.error(f"{box.alias} config pull: {r.stderr.strip()}")
        return r.returncode
    dest = Path(tempfile.gettempdir()) / f"gui-agents-{box.alias}-configs.yaml"
    dest.write_text(r.stdout)
    print(str(dest))
    return 0


def cmd_config_push(args: argparse.Namespace) -> int:
    box = find_by_alias(args.alias)
    src = Path(args.localfile)
    if not src.exists():
        logger.error(f"file not found: {src}")
        return 2
    content = src.read_text()
    r = _ssh_exec(box, "gui-agents-queue config push", stdin=content, timeout=60)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def cmd_config_diff(args: argparse.Namespace) -> int:
    a = find_by_alias(args.aliasA)
    b = find_by_alias(args.aliasB)

    def _pull(box: Box) -> str:
        r = _ssh_exec(box, "cat /var/lib/gui-agents/configs.yaml")
        if r.returncode != 0:
            r = _ssh_exec(
                box,
                "cat \"$(python3 -c 'from infra.worker.queue_cli import CONFIGS_YAML; print(CONFIGS_YAML)')\"",
            )
        return r.stdout if r.returncode == 0 else ""

    ca, cb = _pull(a), _pull(b)
    diff = difflib.unified_diff(
        ca.splitlines(keepends=True),
        cb.splitlines(keepends=True),
        fromfile=f"{a.alias}:configs.yaml",
        tofile=f"{b.alias}:configs.yaml",
    )
    sys.stdout.writelines(diff)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dispatch")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="print state of all boxes")
    st.add_argument("--follow", "-f", action="store_true")

    sh = sub.add_parser("show", help="print full state.json for one box")
    sh.add_argument("alias")

    asg = sub.add_parser("assign", help="push tasks into box queues")
    asg.add_argument("--n", type=int, help="pick N eligible tasks from DB")
    asg.add_argument("--tasks", help="comma-separated explicit task ids")
    asg.add_argument("--agent", help="filter to boxes with this agent_model_name")
    asg.add_argument("--task-source", help="filter tasks by task_source")
    asg.add_argument("--box", help="pin to a single box alias")
    asg.add_argument("-y", "--yes", action="store_true")

    c = sub.add_parser("cancel", help="cancel a queued or running task on a box")
    c.add_argument("alias")
    c.add_argument("task_id")

    cl = sub.add_parser("clear", help="drop the pending queue on a box")
    cl.add_argument("alias")

    lg = sub.add_parser("logs", help="tail journal for a box / task")
    lg.add_argument("alias")
    lg.add_argument("--task")
    lg.add_argument("--follow", "-f", action="store_true")

    lo = sub.add_parser(
        "login", help="open a VNC tunnel to a box for first-time / expired browser login"
    )
    lo.add_argument("alias")
    lo.add_argument("--local-port", type=int, default=5901)
    lo.add_argument(
        "--no-open",
        action="store_true",
        help="don't auto-open the macOS VNC viewer",
    )

    cp = sub.add_parser("config", help="read/write box-local configs.yaml")
    csub = cp.add_subparsers(dest="config_cmd", required=True)
    cpl = csub.add_parser("pull")
    cpl.add_argument("alias")
    cps = csub.add_parser("push")
    cps.add_argument("alias")
    cps.add_argument("localfile")
    cpd = csub.add_parser("diff")
    cpd.add_argument("aliasA")
    cpd.add_argument("aliasB")

    args = p.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd == "assign":
        return cmd_assign(args)
    if args.cmd == "cancel":
        return cmd_cancel(args)
    if args.cmd == "clear":
        return cmd_clear(args)
    if args.cmd == "logs":
        return cmd_logs(args)
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "config":
        if args.config_cmd == "pull":
            return cmd_config_pull(args)
        if args.config_cmd == "push":
            return cmd_config_push(args)
        if args.config_cmd == "diff":
            return cmd_config_diff(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
