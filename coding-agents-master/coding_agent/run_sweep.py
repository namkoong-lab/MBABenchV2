"""Batch entry point: every task in a run config's range, one attempt each.

    python -m coding_agent.run_sweep --config run_configs/offline/<label>.yaml

run_task.py is still the unit of work — a batch decides which task ids to call
it with and in what order, and calls it as its own process, so an attempt
behaves exactly as it does on its own. The task list and the skip-if-attempted
check both follow the config's sink:

  sink: local   tasks from the bundle (data/tasks/), rows already banked read
                from outputs/<agent_model_name>/task_attempts.jsonl
  sink: cloud   tasks from the `tasks` table, rows from `task_attempts`
                (both read-only)

Either way the filters are the benchmark's own: this task source, never a
deprecated row, ascending id, inside the range — `tasks:` in the config,
narrowed by --task-ids or --start/--end.

An attempt that ends as infra_failure writes no row (recorder.RECORDABLE), so
re-running the same command picks it up again — that is the resume path.

--dry-run resolves each task the way a real attempt does (task fetched,
workspace seeded, PROMPT.md written) and prints what the agent would get and
where the result would land, without starting a container.
"""
import argparse
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from .config import TaskRange, load_config, load_dotenv_if_present, resolve_io, resolve_secrets
from .local_sink import attempted_task_ids, cohort_dir
from .run_task import EXIT_CODES, build_source, stage_attempt
from .task_source import local_task_ids

OK_EXITS = {EXIT_CODES["success"], EXIT_CODES["agent_failure"], EXIT_CODES["timeout"]}


def parse_task_ids(spec: str) -> list:
    """"1-101", "3", "1,5,9-12" -> a sorted list of ids."""
    ids = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            first, _, last = part.partition("-")
            ids.update(range(int(first), int(last) + 1))
        else:
            ids.add(int(part))
    return sorted(ids)


def _cloud_task_ids(db_url: str, task_source: str, rng: TaskRange) -> list:
    import psycopg2

    conn = psycopg2.connect(db_url)
    conn.autocommit = True  # read-only; never open a transaction
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM tasks WHERE task_source = %s AND NOT deprecated "
                "AND id BETWEEN %s AND %s ORDER BY id",
                (task_source, rng.first, rng.last),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _cloud_attempted(db_url: str, agent_model_name: str, prompt_version: int) -> set:
    import psycopg2

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT task_id FROM task_attempts "
                "WHERE agent_model_name = %s AND prompt_version = %s AND NOT deprecated",
                (agent_model_name, prompt_version),
            )
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def dry_run_one(cfg, task_id: int) -> int:
    """Resolve one task as far as the container and print what it resolved to."""
    print(f"\n=== task {task_id} (dry run) ===")
    try:
        spec, attempt, seeded, prompt_version = stage_attempt(cfg, build_source(cfg, task_id))
    except Exception as e:  # noqa: BLE001 — same classification a real run gives it
        print(f"❌ infra_failure during task staging: {e}")
        return EXIT_CODES["infra_failure"]
    try:
        prompt = attempt.workspace / "PROMPT.md"
        print(f"  task_name: {spec.task_name} ({spec.task_source})")
        print(f"  PROMPT.md: {prompt} ({prompt.stat().st_size:,} bytes, prompt_version={prompt_version})")
        for rel in sorted(attempt.manifest):
            print(f"  staged: {rel}")
        if seeded:
            print(f"  template attachments: {', '.join(p.name for p in seeded)}")
        if cfg.offline_sink:
            dest = cohort_dir(cfg.local_output_root, cfg.agent_model_name) / f"task_id={task_id}"
            print(f"  would write: {dest}/<YYYYmmdd_HHMMSS>/ (+ a row in "
                  f"{cohort_dir(cfg.local_output_root, cfg.agent_model_name)}/task_attempts.jsonl)")
        else:
            print(f"  would write: s3://{cfg.s3_bucket}/{cfg.s3_root}/attempts/"
                  f"{cfg.agent_model_name}/task_source={spec.task_source}/task_id={task_id}/"
                  f" (+ a task_attempts row)")
        print(f"  would run: {cfg.agent.cli}/{cfg.agent.model} in {cfg.sandbox.image} "
              f"(not started)")
        return 0
    finally:
        shutil.rmtree(attempt.attempt_dir, ignore_errors=True)


def attempt_one(config_path: str, task_id: int, capture: bool) -> tuple:
    """One attempt, as its own run_task process."""
    cmd = [sys.executable, "-m", "coding_agent.run_task",
           "--config", config_path, "--task-id", str(task_id)]
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    return task_id, proc.returncode, (proc.stdout or "") + (proc.stderr or "") if capture else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run every task in a run config's range, one attempt each")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-ids", help='ids to attempt, e.g. "1-101" or "1,5,9-12" '
                                           "(default: the config's tasks range)")
    parser.add_argument("--start", type=int, help="first task id (alternative to --task-ids)")
    parser.add_argument("--end", type=int, help="last task id")
    parser.add_argument("--workers", type=int, default=1,
                        help="attempts to run at once (default 1, sequential)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve every task and print what it would run, start nothing")
    parser.add_argument("--task-source", default="v2", help="benchmark task source (default: v2, the v2 task set)")
    parser.add_argument("--redo", action="store_true",
                        help="attempt every task in range, even ones that already have a row")
    parser.add_argument("--list", action="store_true",
                        help="print the task ids this batch would attempt and exit")
    args = parser.parse_args(argv)

    load_dotenv_if_present()
    cfg = load_config(args.config)
    if cfg.mode != "internal":
        parser.error("run_sweep drives internal runs; external tasks go through run_task --task-dir")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    # Same fail-fast the per-attempt runner does, once instead of per task —
    # except on a dry run, which starts no agent and needs no key.
    resolve_io(cfg) if args.dry_run else resolve_secrets(cfg)

    rng = cfg.tasks or TaskRange()
    if args.start is not None or args.end is not None:
        rng = TaskRange(first=args.start or rng.first, last=args.end or rng.last)
    wanted = set(parse_task_ids(args.task_ids)) if args.task_ids else None

    from .prompt_builder import parse_prompt_version, template_name
    prompt_version = parse_prompt_version(
        cfg.system_prompt, template_name(args.task_source, cfg.template_version))

    if cfg.offline_source:
        ids = local_task_ids(cfg.local_data_root, args.task_source, rng.first, rng.last)
    else:
        ids = _cloud_task_ids(cfg.db_url, args.task_source, rng)
    if wanted is not None:
        ids = [i for i in ids if i in wanted]

    if args.redo or args.dry_run:
        done = set()
    elif cfg.offline_sink:
        done = attempted_task_ids(cfg.local_output_root, cfg.agent_model_name, prompt_version)
    else:
        done = _cloud_attempted(cfg.db_url, cfg.agent_model_name, prompt_version)

    todo = [i for i in ids if i not in done]
    print(f"▶ {cfg.agent_model_name} pv={prompt_version} tasks {rng} "
          f"source={cfg.io_source} sink={cfg.io_sink}"
          + (" (dry run)" if args.dry_run else f" workers={args.workers}"))
    print(f"  {len(ids)} in range, {len(ids) - len(todo)} already recorded, {len(todo)} to run")
    if args.list:
        print(" ".join(str(i) for i in todo))
        return 0

    if args.dry_run:
        worst = max((dry_run_one(cfg, i) for i in todo), default=0)
        print(f"\n▶ dry run finished: {len(todo)} tasks resolved, nothing started")
        return 0 if worst == 0 else worst

    results = {}
    if args.workers == 1:
        for n, task_id in enumerate(todo, 1):
            print(f"\n=== [{n}/{len(todo)}] task {task_id} ===", flush=True)
            results[task_id] = attempt_one(args.config, task_id, capture=False)[1]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(attempt_one, args.config, i, True) for i in todo]
            for n, future in enumerate(futures, 1):
                task_id, code, output = future.result()
                print(f"\n=== [{n}/{len(todo)}] task {task_id} -> exit {code} ===", flush=True)
                print(output, end="", flush=True)
                results[task_id] = code

    bad = {i: c for i, c in results.items() if c not in OK_EXITS}
    print(f"\n▶ batch finished: {len(todo)} attempted, {len(bad)} ended on infra/review "
          f"({', '.join(f'{i}:{c}' for i, c in sorted(bad.items())) or 'none'})")
    return 0 if not bad else max(bad.values())


if __name__ == "__main__":
    sys.exit(main())
