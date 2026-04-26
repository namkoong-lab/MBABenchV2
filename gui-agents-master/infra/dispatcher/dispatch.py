"""Laptop-side dispatcher CLI.

Fans out over SSH to read box state and push task assignments. No central
daemon, no DB-backed message queue — each invocation reads/writes box state
over SSH and exits.

Commands:
    dispatch status [--follow]
    dispatch show <alias>
    dispatch assign --n N [--agent X] [--task-source Y]
    dispatch assign --tasks 42,43,44 [--box <alias>]
    dispatch backlog [--agent X] [--task-source Y]
    dispatch cancel <alias> <task_id>
    dispatch clear <alias>
    dispatch logs <alias> [--task <id>] [-f]
    dispatch login <alias> [--local-port N] [--no-open]
    dispatch probe [<alias> | --all]
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
from infra.dispatcher.diagnostics import (  # noqa: E402
    ConnectivityVerdict,
    diagnose_connectivity,
)

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


def _ssh_exec(
    box: Box, remote_cmd: str, stdin: str | None = None, timeout: int = SSH_TIMEOUT_SEC
) -> SSHResult:
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


_AUTH_STALE_AFTER_SEC = 30 * 60  # checked_at older than this → "old …"
_AUTH_COL_WIDTH = 26


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"


def _is_auth_old(auth: dict | None, busy: bool = False) -> bool:
    """True when the last probe succeeded but is older than _AUTH_STALE_AFTER_SEC.

    Mirrors the "old" branch in _fmt_auth. STALE (failed probe) and "?"
    (no probe) are NOT considered old — those need `dispatch login`, not
    just a re-probe.

    `busy=True` suppresses the "old" signal: when the worker has a
    current task, the auth-probe oneshot short-circuits (auth_probe.py
    skips while a task is running to avoid racing with the agent over
    the shared Chrome). Staleness during that window is expected and
    un-actionable, so we don't flag it.
    """
    if busy:
        return False
    if not auth or not auth.get("ok"):
        return False
    checked_at = auth.get("checked_at") or ""
    try:
        when = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_s = (datetime.now(timezone.utc) - when).total_seconds()
    return age_s > _AUTH_STALE_AFTER_SEC


def _kick_probe(box: Box) -> tuple[bool, str]:
    """Fire the gui-agents-auth-probe oneshot on a box.

    systemctl start on a oneshot blocks until the unit finishes, so when
    this returns the box's auth.json is up to date.
    """
    try:
        r = _ssh_exec(
            box, "sudo -n systemctl start gui-agents-auth-probe.service", timeout=60
        )
    except subprocess.TimeoutExpired:
        return False, "ssh timed out"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f"rc={r.returncode}"
    return True, "ok"


def _fmt_auth(auth: dict | None, busy: bool = False) -> str:
    """Render the auth-probe column. Values:
    <email>       — last probe succeeded and is fresh (shows user identity)
    ok            — succeeded but no email available (e.g. claude)
    STALE         — last probe failed (any reason) — needs re-login
    old <email>   — succeeded but last probe was too long ago
    ?             — no probe result on this box

    `busy=True` suppresses the "old" prefix: the probe oneshot skips
    while the worker has a current task (it would race with the agent
    over the shared Chrome), so staleness is expected and un-actionable
    during that window. We keep the last known identity visible.
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
    email = auth.get("email") or ""
    stale = (age_s is not None and age_s > _AUTH_STALE_AFTER_SEC) and not busy
    if email:
        label = f"old {email}" if stale else email
    else:
        label = "old" if stale else "ok"
    return _truncate(label, _AUTH_COL_WIDTH)


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
    login_s = _fmt_auth(auth, busy=bool(current))
    return (
        f"  {alias:<12} {wid:<20} {agent:<14} {pv_s:<5} "
        f"{login_s:<{_AUTH_COL_WIDTH}} {cur_s:<24} +{len(queue):<3} {last_s}"
    )


def _print_status_table(boxes: list[Box], states: dict[str, dict | None]) -> None:
    header = (
        f"  {'alias':<12} {'worker_id':<20} {'agent':<14} {'pv':<5} "
        f"{'login':<{_AUTH_COL_WIDTH}} {'current':<24} {'q':<4} last"
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
    exclude_ids: list[int] | None = None,
    skip_deprecated: bool = True,
    skip_already_attempted: bool = True,
) -> list[dict]:
    """Query the Bizbench `tasks` table for eligible rows. Mirrors the
    filters used by BizbenchPostgresS3TaskSource, minus file download.

    `exclude_ids` subtracts task ids already in-flight (current + queued)
    across the cohort — those aren't in `task_attempts` yet, so the
    anti-join doesn't catch them and we'd otherwise re-pick them. Same
    pattern as `_count_eligible_tasks`, so `backlog` and `assign` agree
    on what 'unassigned' means."""
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
    if exclude_ids:
        where.append("NOT (t.id = ANY(%(exclude_ids)s))")
        params["exclude_ids"] = list(exclude_ids)

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


def _count_eligible_tasks(
    *,
    agent_model_name: str,
    prompt_version: int | str | None,
    task_sources: list[str] | None,
    exclude_ids: list[int] | None = None,
    skip_deprecated: bool = True,
    skip_already_attempted: bool = True,
) -> int:
    """COUNT(*) variant of `_list_eligible_tasks`. `exclude_ids` subtracts
    task ids already in-flight (current + queued) across the cohort — those
    are not yet in `task_attempts`, so the anti-join doesn't catch them."""
    import psycopg2
    from psycopg2 import sql

    where = ["TRUE"]
    params: dict = {}
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
    if exclude_ids:
        where.append("NOT (t.id = ANY(%(exclude_ids)s))")
        params["exclude_ids"] = list(exclude_ids)

    q = sql.SQL("SELECT COUNT(*) FROM tasks t WHERE {where}").format(
        where=sql.SQL(" AND ").join(sql.SQL(w) for w in where)
    )

    conn = psycopg2.connect(_get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(q, params)
            row = cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        conn.close()


def _fetch_tasks_by_ids(task_ids: list[str]) -> dict[str, dict]:
    """Look up rows in the `tasks` table by id. Returns {str(id): row}.
    Missing ids (including non-int strings) are simply absent from the
    result — the caller falls back to a blank label."""
    import psycopg2
    import psycopg2.extras

    int_ids: list[int] = []
    for tid in task_ids:
        try:
            int_ids.append(int(tid))
        except (TypeError, ValueError):
            continue
    if not int_ids:
        return {}
    conn = psycopg2.connect(_get_db_url())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, task_name, task_source FROM tasks WHERE id = ANY(%s)",
                (int_ids,),
            )
            return {str(r["id"]): dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


def _in_flight_ids_from_states(
    boxes: list[Box], states: dict[str, dict | None]
) -> set[int]:
    """Union of `current.task_id` + queued `task_id`s across the given
    boxes, coerced to int. Silently drops non-int ids."""
    out: set[int] = set()
    for b in boxes:
        st = states.get(b.alias)
        if st is None:
            continue
        current = st.get("current") or {}
        candidates: list = [current.get("task_id")]
        for q in st.get("queue") or []:
            candidates.append(q.get("task_id"))
        for tid in candidates:
            if tid is None:
                continue
            try:
                out.add(int(tid))
            except (TypeError, ValueError):
                continue
    return out


def _parse_prompt_version(pv: str) -> int | str | None:
    """Decode the stringified prompt_version used as a cohort key."""
    if pv == "None":
        return None
    try:
        return int(pv)
    except ValueError:
        return pv


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    boxes = load_boxes()
    if not boxes:
        logger.error("no boxes in registry")
        return 2
    if diagnose_connectivity() is ConnectivityVerdict.IP_BLOCKED:
        logger.error(
            "skipping SSH fan-out (IP blocked). "
            "Set DISPATCH_NO_DIAGNOSE=1 to force."
        )
        return 2
    if args.follow:
        try:
            while True:
                states = _fetch_all_states(boxes)
                print("\033[2J\033[H", end="")  # clear screen
                print(
                    f"dispatch status — {datetime.now().isoformat(timespec='seconds')}\n"
                )
                _print_status_table(boxes, states)
                time.sleep(5)
        except KeyboardInterrupt:
            return 0
    else:
        states = _fetch_all_states(boxes)
        _print_status_table(boxes, states)
        _prompt_probe_old(boxes, states)
    return 0


def _prompt_probe_old(boxes: list[Box], states: dict[str, dict | None]) -> None:
    """After printing the status table, offer to re-probe any boxes whose
    login column showed 'old'. Skipped when stdin isn't a TTY so piped
    invocations (scripts, `watch`, etc.) don't stall on input."""
    old = [
        b for b in boxes
        if _is_auth_old(
            (states.get(b.alias) or {}).get("auth"),
            busy=bool((states.get(b.alias) or {}).get("current")),
        )
    ]
    if not old:
        return
    if not sys.stdin.isatty():
        return
    aliases = ", ".join(b.alias for b in old)
    try:
        resp = input(
            f"\n{len(old)} box(es) show an 'old' login ({aliases}). "
            f"Kick a fresh auth probe on them now? [y/N]: "
        ).strip().lower()
    except EOFError:
        return
    if resp not in {"y", "yes"}:
        return
    for b in old:
        ok, msg = _kick_probe(b)
        if ok:
            logger.info(f"{b.alias}: probe fired")
        else:
            logger.warning(f"{b.alias}: probe kick failed: {msg}")


def cmd_show(args: argparse.Namespace) -> int:
    box = find_by_alias(args.alias)
    if diagnose_connectivity() is ConnectivityVerdict.IP_BLOCKED:
        logger.error(
            f"skipping SSH to {box.alias} (IP blocked). "
            "Set DISPATCH_NO_DIAGNOSE=1 to force."
        )
        return 2
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

    if diagnose_connectivity() is ConnectivityVerdict.IP_BLOCKED:
        logger.error(
            "skipping assign (IP blocked — boxes are unreachable). "
            "Set DISPATCH_NO_DIAGNOSE=1 to force."
        )
        return 2
    states = _fetch_all_states(boxes)
    reachable = [b for b in boxes if states.get(b.alias) is not None]
    if not reachable:
        logger.error("no reachable boxes")
        return 2

    # Populated on the --n path; empty on --tasks. After the SSH loop we
    # print "remaining after this batch: N" per touched cohort.
    touched_cohorts: dict[tuple[str, int | str | None], dict] = {}
    _box_cohort: dict[str, tuple[str, int | str | None]] = {}
    assign_task_sources: list[str] | None = (
        [args.task_source] if args.task_source else None
    )

    # Explicit task IDs — resolve names by DB lookup so the box gets a
    # readable label; fall back to empty name on missing row.
    if args.tasks:
        task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
        try:
            details = _fetch_tasks_by_ids(task_ids)
        except Exception as e:
            logger.warning(f"task name lookup failed; proceeding without names: {e}")
            details = {}
        tasks = [
            {
                "id": tid,
                "task_name": (details.get(tid) or {}).get("task_name"),
                "task_source": (details.get(tid) or {}).get("task_source"),
            }
            for tid in task_ids
        ]
        # --agent filters the candidate box pool. The --n path does this via
        # _group_boxes_by_agent; on the --tasks path we just intersect
        # reachable against the requested agent.
        candidates = reachable
        if args.agent:
            candidates = [
                b
                for b in reachable
                if ((states.get(b.alias) or {}).get("worker") or {}).get(
                    "agent_model_name"
                )
                == args.agent
            ]
            if not candidates:
                logger.error(
                    f"no reachable boxes match --agent={args.agent}"
                )
                return 2
        assignments: list[tuple[Box, dict]] = []
        if args.box:
            for t in tasks:
                assignments.append((candidates[0], t))
        else:
            assignments = _distribute_least_loaded(tasks, candidates, states)
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
            pv_val = _parse_prompt_version(pv)
            in_flight = _in_flight_ids_from_states(group_boxes, states)
            tasks = _list_eligible_tasks(
                agent_model_name=agent,
                prompt_version=pv_val,
                task_sources=assign_task_sources,
                limit=args.n,
                exclude_ids=sorted(in_flight) if in_flight else None,
            )
            logger.info(
                f"group agent={agent} pv={pv}: {len(tasks)} eligible task(s), "
                f"{len(group_boxes)} box(es), "
                f"{len(in_flight)} already in-flight (excluded)"
            )
            cohort_assignments = _distribute_least_loaded(tasks, group_boxes, states)
            assignments += cohort_assignments
            touched_cohorts[(agent, pv_val)] = {
                "pre_in_flight": in_flight,
                "added": set(),  # populated below as SSH adds succeed
            }
    # Reverse lookup from "box alias" → cohort key, so we can record
    # successful adds against the right cohort in the SSH loop below.
    _box_cohort: dict[str, tuple[str, int | str | None]] = {}
    if not args.tasks:
        for cohort_key, _grp in groups.items():
            agent, pv = cohort_key
            for gb in _grp:
                _box_cohort[gb.alias] = (agent, _parse_prompt_version(pv))

    if not assignments:
        logger.info("nothing to assign")
        return 0

    print(f"\nAssigning {len(assignments)} task(s):")
    for box, task in assignments:
        print(
            f"  -> {box.alias:<12} task={task.get('id')} name={task.get('task_name') or ''}"
        )
    if not args.yes:
        try:
            resp = input("\nProceed? [y/N]: ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in {"y", "yes"}:
            logger.info("aborted")
            return 0

    failures = 0
    noops = 0
    for box, task in assignments:
        tid = str(task.get("id"))
        name = (task.get("task_name") or "").replace("'", "")
        remote = f"gui-agents-queue add {tid}" + (f" '{name}'" if name else "")
        r = _ssh_exec(box, remote)
        if r.returncode != 0:
            failures += 1
            logger.error(f"{box.alias} add {tid} failed: {r.stderr.strip()}")
            continue
        # `gui-agents-queue add` exits 0 with a stderr note when the task
        # is already queued/running on the box. Surface it instead of
        # falsely reporting "add ok" — and don't count it as an add.
        stderr = (r.stderr or "").strip()
        if "already queued or running" in stderr:
            noops += 1
            logger.warning(f"{box.alias} add {tid} no-op: {stderr}")
            continue
        logger.info(f"{box.alias} add {tid} ok")
        cohort_key = _box_cohort.get(box.alias)
        if cohort_key is not None:
            try:
                touched_cohorts[cohort_key]["added"].add(int(tid))
            except (KeyError, TypeError, ValueError):
                pass
    if noops:
        logger.warning(
            f"{noops} assignment(s) were no-ops (task already queued/running). "
            f"This usually means the dispatcher's view of in-flight state was "
            f"stale; re-run `dispatch status` and try again."
        )

    # Post-batch summary: one extra COUNT per touched cohort so operators
    # can see "did I drain the queue?" without a second command.
    if touched_cohorts:
        print()
        for (agent, pv_val), info in touched_cohorts.items():
            exclude = info["pre_in_flight"] | info["added"]
            try:
                remaining = _count_eligible_tasks(
                    agent_model_name=agent,
                    prompt_version=pv_val,
                    task_sources=assign_task_sources,
                    exclude_ids=sorted(exclude) or None,
                )
            except Exception as e:
                logger.warning(f"backlog count failed for agent={agent} pv={pv_val}: {e}")
                continue
            pv_s = f"v{pv_val}" if pv_val is not None else "-"
            print(
                f"  remaining after this batch: agent={agent} pv={pv_s} "
                f"-> {remaining} eligible"
            )
    return 1 if failures else 0


def cmd_backlog(args: argparse.Namespace) -> int:
    """Per-cohort COUNT of eligible tasks. For each cohort of reachable
    boxes sharing (agent_model_name, prompt_version), prints:

      in_flight  — tasks already queued/running across the cohort
      unassigned — tasks matching the assign filters, minus in_flight
      remaining  — in_flight + unassigned (total work left for this cohort)
      total      — all DB rows in cohort scope (non-deprecated, task_source
                   filter), including ones already successfully attempted

    `unassigned` / `remaining` reuse the same eligibility filters as `assign`;
    `total` drops the `skip_already_attempted` anti-join so it reflects the
    full universe of tasks the cohort could ever see.
    """
    boxes = load_boxes()
    if not boxes:
        logger.error("no boxes in registry")
        return 2
    if diagnose_connectivity() is ConnectivityVerdict.IP_BLOCKED:
        logger.error(
            "skipping backlog (IP blocked — boxes are unreachable). "
            "Set DISPATCH_NO_DIAGNOSE=1 to force."
        )
        return 2
    states = _fetch_all_states(boxes)
    reachable = [b for b in boxes if states.get(b.alias) is not None]
    unreachable = [b.alias for b in boxes if states.get(b.alias) is None]
    if not reachable:
        logger.error("no reachable boxes")
        return 2

    groups = _group_boxes_by_agent(reachable, states)
    if args.agent:
        groups = {k: v for k, v in groups.items() if k[0] == args.agent}
    if not groups:
        logger.error("no reachable boxes match the agent filter")
        return 2

    task_sources = [args.task_source] if args.task_source else None

    header = (
        f"  {'agent':<20} {'pv':<6} {'boxes':<6} "
        f"{'in_flight':<10} {'unassigned':<11} {'remaining':<10} total"
    )
    print()
    print(header)
    print("  " + "-" * (len(header) - 2))
    any_err = False
    for (agent, pv), group_boxes in sorted(groups.items()):
        pv_val = _parse_prompt_version(pv)
        in_flight_ids = _in_flight_ids_from_states(group_boxes, states)
        try:
            unassigned = _count_eligible_tasks(
                agent_model_name=agent,
                prompt_version=pv_val,
                task_sources=task_sources,
                exclude_ids=sorted(in_flight_ids) or None,
            )
            total = _count_eligible_tasks(
                agent_model_name=agent,
                prompt_version=pv_val,
                task_sources=task_sources,
                skip_already_attempted=False,
            )
        except Exception as e:
            logger.error(f"count failed for agent={agent} pv={pv}: {e}")
            any_err = True
            continue
        in_flight = len(in_flight_ids)
        remaining = in_flight + unassigned
        pv_s = f"v{pv}" if pv != "None" else "-"
        print(
            f"  {agent:<20} {pv_s:<6} {len(group_boxes):<6} "
            f"{in_flight:<10} {unassigned:<11} {remaining:<10} {total}"
        )

    print()
    print(
        "  note: snapshot — workers may finish tasks between state fetch and COUNT."
    )
    if unreachable:
        print(
            f"  note: {len(unreachable)} unreachable box(es) skipped; their "
            f"queued tasks may inflate 'unassigned': {', '.join(unreachable)}"
        )
    return 1 if any_err else 0


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
    return subprocess.call(
        ["ssh", "-t", *box.ssh_base_args(), box.ssh_target(), remote]
    )


def cmd_login(args: argparse.Namespace) -> int:
    """Open a VNC tunnel to a box so the operator can log in to
    claude.ai / chatgpt.com.

    Drives the worker's existing Chrome (managed by
    gui-agents-chrome.service) via its configured CDP port — so cookies
    land in the same profile the auth probe reads, and the login window
    the operator sees IS the browser the probe targets. After the VNC
    session ends, fires the auth-probe oneshot so `dispatch status`
    reflects the new login state immediately instead of waiting for
    the 5-minute timer.
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
from infra.configs import load_configs
c = load_configs()
prov = c.provider.kind
block = c.claude_web if prov == "claude" else c.chatgpt_web
port = int(block.browser.cdp_port)
url = "https://chatgpt.com/" if prov == "chatgpt" else "https://claude.ai/"
print(f"PROVIDER={{prov}}")
print(f"CDP_PORT={{port}}")
print(f"LOGIN_URL={{url}}")
') || {{ echo "ERROR: failed to read box config" >&2; exit 1; }}
eval "$EVAL_OUT"
export PROVIDER CDP_PORT LOGIN_URL
echo "INFO: provider=$PROVIDER cdp_port=$CDP_PORT" >&2

if ! sudo -n true 2>/dev/null; then
  echo "ERROR: passwordless sudo required (worker Chrome runs as root)" >&2
  exit 1
fi

# Ensure the worker's Chrome is up. Idempotent — no-op if already active.
sudo -n systemctl start gui-agents-chrome.service || {{
  echo "ERROR: could not start gui-agents-chrome.service; check journalctl -u gui-agents-chrome.service" >&2
  exit 1
}}

# Wait up to 20s for the CDP port to bind.
for _ in $(seq 1 40); do
  if (echo >/dev/tcp/127.0.0.1/$CDP_PORT) 2>/dev/null; then break; fi
  sleep 0.5
done
if ! (echo >/dev/tcp/127.0.0.1/$CDP_PORT) 2>/dev/null; then
  echo "ERROR: worker Chrome CDP port $CDP_PORT not reachable. Check \\`systemctl status gui-agents-chrome.service\\`." >&2
  exit 1
fi

# Open the login URL as a new page inside the worker's Chrome via CDP.
# browser.close() here just disconnects the CDP client — the page stays
# open in Chrome for the operator to interact with over VNC.
python3 - <<'PY' || {{ echo "ERROR: failed to open login page over CDP" >&2; exit 1; }}
import os, sys, time
from playwright.sync_api import sync_playwright
cdp_port = int(os.environ["CDP_PORT"])
login_url = os.environ["LOGIN_URL"]
last_err = None
for _ in range(20):
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{{cdp_port}}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                print(f"WARN: goto failed: {{e}}", file=sys.stderr)
            try:
                page.bring_to_front()
            except Exception:
                pass
            browser.close()
        last_err = None
        break
    except Exception as e:
        last_err = e
        time.sleep(0.5)
if last_err is not None:
    print(f"ERROR: CDP connect failed: {{last_err}}", file=sys.stderr)
    sys.exit(1)
PY

cleanup() {{
  pkill -x x11vnc >/dev/null 2>&1 || true
}}
trap cleanup EXIT INT TERM HUP

pkill -x x11vnc >/dev/null 2>&1 || true
sleep 0.2
x11vnc -display :99 -localhost -passwd '{vnc_pw}' -rfbport {remote_port} -forever -shared -quiet
"""
    ssh_args = [
        "ssh",
        "-t",
        "-L",
        f"{local_port}:localhost:{remote_port}",
        *box.ssh_base_args(),
        box.ssh_target(),
        remote_cmd,
    ]
    logger.info(
        f"{box.alias}: opening login page in the worker's Chrome via CDP, "
        f"x11vnc on :99 forwarded to localhost:{local_port}"
    )
    logger.info(
        f"connect a VNC viewer to vnc://localhost:{local_port} — "
        f"log in to claude.ai / chatgpt.com in the Chrome window you see, "
        f"then Ctrl-C here to tear down."
    )
    logger.info(f"VNC password (one-shot): {vnc_pw}")
    if sys.platform == "darwin" and not args.no_open:
        # Playwright's cold start on the box (~15–25s) means x11vnc doesn't
        # bind for a while after ssh connects. SSH -L's local port accepts
        # connections immediately so a plain socket-connect check isn't
        # enough — we probe for the RFB greeting, which only shows up once
        # x11vnc is actually listening end-to-end.
        import socket
        import threading

        def _open_viewer() -> None:
            deadline = time.time() + 90
            while time.time() < deadline:
                try:
                    with socket.create_connection(
                        ("localhost", local_port), timeout=2
                    ) as s:
                        s.settimeout(3)
                        if s.recv(4) == b"RFB ":
                            break
                except OSError:
                    pass
                time.sleep(1)
            else:
                logger.warning(
                    f"VNC not ready on localhost:{local_port} after 90s; "
                    f"run `open vnc://localhost:{local_port}` manually."
                )
                return
            # Give x11vnc a beat to finish closing the probe session before
            # Screen Sharing's new connection lands — otherwise the viewer
            # can race x11vnc's accept loop and hit "connection failed".
            time.sleep(2)
            logger.info(
                f"VNC ready; opening Screen Sharing at vnc://localhost:{local_port}"
            )
            subprocess.run(["open", f"vnc://localhost:{local_port}"], check=False)

        threading.Thread(target=_open_viewer, daemon=True).start()
    rc = subprocess.call(ssh_args)

    # Kick a fresh probe so `dispatch status` reflects the new login
    # state immediately. systemctl start on a oneshot blocks until the
    # unit finishes, so when this returns auth.json is up to date.
    logger.info(f"{box.alias}: running auth probe to refresh status …")
    probe = _ssh_exec(
        box, "sudo -n systemctl start gui-agents-auth-probe.service", timeout=60
    )
    if probe.returncode != 0:
        logger.warning(
            f"{box.alias}: auth-probe kick returned {probe.returncode}: "
            f"{(probe.stderr or probe.stdout).strip()}"
        )
    return rc


def cmd_probe(args: argparse.Namespace) -> int:
    """Kick the auth-probe oneshot on one box or every registered box.

    A cheaper alternative to `dispatch login` when a box's login column
    shows 'old' — the cookie is still good, it just hasn't been re-verified
    recently. STALE entries (probe failed) still need `dispatch login`.
    """
    if diagnose_connectivity() is ConnectivityVerdict.IP_BLOCKED:
        logger.error(
            "skipping SSH fan-out (IP blocked). "
            "Set DISPATCH_NO_DIAGNOSE=1 to force."
        )
        return 2
    if args.all:
        boxes = load_boxes()
        if not boxes:
            logger.error("no boxes in registry")
            return 2
    else:
        if not args.alias:
            logger.error("specify an <alias> or pass --all")
            return 2
        boxes = [find_by_alias(args.alias)]
    failures = 0
    for box in boxes:
        ok, msg = _kick_probe(box)
        if ok:
            logger.info(f"{box.alias}: probe fired")
        else:
            logger.warning(f"{box.alias}: probe kick failed: {msg}")
            failures += 1
    return 0 if failures == 0 else 1


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

    bl = sub.add_parser(
        "backlog",
        help="per-cohort COUNT of eligible tasks (in_flight + unassigned + total)",
    )
    bl.add_argument("--agent", help="filter to boxes with this agent_model_name")
    bl.add_argument("--task-source", help="filter tasks by task_source")

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
        "login",
        help="open a VNC tunnel to a box for first-time / expired browser login",
    )
    lo.add_argument("alias")
    lo.add_argument("--local-port", type=int, default=5901)
    lo.add_argument(
        "--no-open",
        action="store_true",
        help="don't auto-open the macOS VNC viewer",
    )

    pr = sub.add_parser(
        "probe",
        help="kick the auth-probe oneshot to refresh a box's login status",
    )
    pr.add_argument("alias", nargs="?")
    pr.add_argument("--all", action="store_true", help="probe every registered box")

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
    if args.cmd == "backlog":
        return cmd_backlog(args)
    if args.cmd == "cancel":
        return cmd_cancel(args)
    if args.cmd == "clear":
        return cmd_clear(args)
    if args.cmd == "logs":
        return cmd_logs(args)
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "probe":
        return cmd_probe(args)
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
