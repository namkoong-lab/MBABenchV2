"""`gui-agents-queue` — the SSH-reachable entry point for reading and
mutating state.json on a single box.

The laptop's dispatcher calls this over SSH; the worker loop on the box
also calls the same helpers from `state.py`. Both go through the flock in
`locked_state()`, so mutations are serialized.

Commands:
    gui-agents-queue show
    gui-agents-queue add <task_id> [<task_name>]
    gui-agents-queue remove <task_id>
    gui-agents-queue clear
    gui-agents-queue config show
    gui-agents-queue config push                     # reads YAML on stdin
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import yaml

from . import state as S
from .auth_probe import read_auth

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_YAML = _REPO_ROOT / "infra" / "configs" / "configs.yaml"
WORKER_SERVICE = "gui-agents-worker.service"


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_show() -> int:
    payload = S.read_state().to_dict()
    # Fold the auth-probe result into the same blob so the dispatcher
    # only needs one SSH round-trip to render the status table.
    auth = read_auth()
    if auth is not None:
        payload["auth"] = auth
    _print_json(payload)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    ok = S.enqueue(args.task_id, args.task_name)
    if not ok:
        print(
            f"task_id={args.task_id} already queued or running; no-op",
            file=sys.stderr,
        )
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    removed = S.remove_from_queue(args.task_id)
    if not removed:
        print(f"task_id={args.task_id} not in queue", file=sys.stderr)
        return 1
    return 0


def cmd_clear() -> int:
    n = S.clear_queue()
    print(f"dropped {n} queued task(s)")
    return 0


def cmd_config_show() -> int:
    _print_json(asdict(S.read_state().worker))
    return 0


def cmd_config_push() -> int:
    """Replace configs.yaml with stdin content and restart the worker service.
    The new content is validated as parseable YAML before replacing the
    existing file; restart is skipped if validation fails."""
    new_content = sys.stdin.read()
    try:
        parsed = yaml.safe_load(new_content)
    except yaml.YAMLError as e:
        print(f"config push rejected: YAML parse error: {e}", file=sys.stderr)
        return 2
    if parsed is not None and not isinstance(parsed, dict):
        print(
            "config push rejected: configs.yaml must be a mapping at top level",
            file=sys.stderr,
        )
        return 2

    CONFIGS_YAML.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(CONFIGS_YAML.parent),
        prefix=".configs.yaml.",
        suffix=".new",
        delete=False,
    ) as tf:
        tf.write(new_content)
        tmp = Path(tf.name)
    os.replace(tmp, CONFIGS_YAML)
    print(f"wrote {CONFIGS_YAML}")

    if shutil.which("systemctl") is None:
        print(
            "systemctl not found — skipping worker restart. Restart the "
            "worker service manually for the new config to take effect.",
            file=sys.stderr,
        )
        return 0
    proc = subprocess.run(
        ["systemctl", "restart", WORKER_SERVICE],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(
            f"systemctl restart {WORKER_SERVICE} failed "
            f"(exit={proc.returncode}): {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return proc.returncode
    print(f"restarted {WORKER_SERVICE}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gui-agents-queue")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="print full state.json")

    a = sub.add_parser("add", help="enqueue a task")
    a.add_argument("task_id")
    a.add_argument("task_name", nargs="?", default=None)

    r = sub.add_parser("remove", help="drop a task from the queue")
    r.add_argument("task_id")

    sub.add_parser("clear", help="drop the entire pending queue")

    cp = sub.add_parser("config", help="box-local configs.yaml helpers")
    csub = cp.add_subparsers(dest="config_cmd", required=True)
    csub.add_parser("show", help="print the worker config summary")
    csub.add_parser("push", help="read YAML on stdin, replace configs.yaml, restart worker")

    args = p.parse_args(argv)

    if args.cmd == "show":
        return cmd_show()
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "remove":
        return cmd_remove(args)
    if args.cmd == "clear":
        return cmd_clear()
    if args.cmd == "config":
        if args.config_cmd == "show":
            return cmd_config_show()
        if args.config_cmd == "push":
            return cmd_config_push()
    return 2


if __name__ == "__main__":
    sys.exit(main())
