"""task-io-driven runner for excel-agents.

Reads configs from infra/configs/, builds a TaskSource + AttemptSink, and
drives the excel_agent/engine.py subprocess per task.

Config layering (later wins), all of it project-wide:

    1. infra/configs/configs.default.yaml   (schema + defaults)
    2. infra/configs/configs.yaml           (gitignored machine overrides)
    3. --run-config <file>                  (the per-experiment overlay)

Credentials are NOT part of that stack. The database url and AWS keys come
from the monorepo config at <repo>/config/config.yaml, with the url picked
by `benchmark` (v1 -> database.v1_url, v2 -> database.v2_url). See
task_io/registry.py for the full precedence.

ATTEMPT SEMANTICS (coding-agents style):
    * engine exit 0 (success) and 1 (agent failure — prompt_failed/timeout)
      are REAL attempts: published to the sink, agent failures with
      agent_failed=true.
    * engine exit 3 (infra: nav/Excel/panel/download) and the runner's own
      deadman kill (rc 124) are NOT attempts: nothing is published, no
      trial is burned, and the task is retried in place up to
      runner.max_infra_tries times. Still failing, the task is skipped for
      this invocation (it stays eligible for future runs) and counted in
      the summary.
    * engine exit 2 (config error) aborts the whole run — every task would
      fail the same way.

Usage (from excel-agents-master/):
    python -m infra.run --dry-run --run-config <file>   # ALWAYS dry-run first
    python -m infra.run --run-config <file>
    python -m infra.run --task-id 42 --run-config <file>

Exit codes:
    0   ran >=1 task; every attempt succeeded and nothing was infra-skipped
    1   ran >=1 task; >=1 agent failure or infra-skipped task
    2   config / preflight / CLI error — nothing was attempted
    3   the source yielded no tasks
    4   environment gate blocked the run (CDP lock held by another run)
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from excel_agent.core.file_manager import FileManager
from task_io import (
    AttemptResult,
    TaskSpec,
    build_sink,
    build_source,
    describe_database_target,
)

from infra.configs import (
    AgentIdentityError,
    ConfigError,
    describe_prompt_version,
    load_configs,
    resolve_agent_identity,
    resolve_prompt_files,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("infra.run")

# Exit codes — see the module docstring.
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_NO_TASKS = 3
EXIT_ENV_BLOCKED = 4

# Engine exit codes (excel_agent/engine.py — keep in sync).
ENGINE_SUCCESS = 0
ENGINE_AGENT_FAILURE = 1
ENGINE_CONFIG_ERROR = 2
ENGINE_INFRA_FAILURE = 3
# run_engine's sentinel for "engine exceeded the deadman and was killed".
ENGINE_RC_TIMEOUT = 124

# If a --run-config file has any of these at top level, treat it as a YAML
# task file (hand it to YamlTaskSource) instead of a project-wide overlay.
_RUN_CONFIG_TASK_KEYS = {
    "task_name",
    "upload_files",
    "solution_name",
    "skip",
    "task_source",
    "tasks",
}


def _sanitize_name(value: str) -> str:
    """Filesystem-safe task-name stem for staging directories."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (value or ""))


def _ns_to_dict(obj):
    if isinstance(obj, SimpleNamespace):
        return {k: _ns_to_dict(v) for k, v in vars(obj).items()}
    if isinstance(obj, list):
        return [_ns_to_dict(v) for v in obj]
    return obj


def _load_prompt_texts(prompt_files: list[str]) -> list[str]:
    """The prompt text the engine will send, in order. The registry's file
    paths are repo-root-relative."""
    texts = []
    for raw in prompt_files:
        p = Path(raw)
        if not p.is_absolute():
            p = _REPO_ROOT / p
        texts.append(p.read_text())
    return texts


def build_engine_config(
    cfg: SimpleNamespace,
    spec: TaskSpec,
    identity,
    prompt_texts: list[str],
    attempt_number: int = 0,
) -> dict:
    """Assemble the engine-input dict for one task.

    The workbook among the task's starting files becomes `template_file`
    (its NAME — the provisioning script placed a file with that name in the
    task's OneDrive folder); every other starting file is uploaded into the
    add-in panel. The identity's pinned UI axes are injected into the
    provider block, where the cores select AND verify them.
    """
    base = _ns_to_dict(cfg)
    provider = identity.provider

    local_paths = [str(p) for p in spec.upload_files]
    task_source = (
        spec.metadata.get("task_source") if isinstance(spec.metadata, dict) else None
    ) or ""
    workbook = FileManager.find_workbook_file(local_paths, task_source)
    panel_files = FileManager.get_files_to_upload(local_paths, workbook)

    provider_block = copy.deepcopy(base.get(provider, {}) or {})
    if identity.ui_model_label is not None:
        provider_block["ui_model_label"] = identity.ui_model_label
    if identity.thinking_effort is not None:
        provider_block["thinking_effort"] = identity.thinking_effort

    engine_config: dict = {
        "agent_type": provider,
        "task_name": spec.task_name,
        "task_id": spec.task_id,
        "task_source": task_source,
        "prompts": list(prompt_texts),
        "prompt_version": base.get("prompt_version"),
        "file_path": list(base.get("onedrive_base_path") or []),
        "template_file": Path(workbook).name if workbook else None,
        "upload_files": panel_files,
        "attempt_number": attempt_number,
        "browser": copy.deepcopy(base.get("browser", {}) or {}),
        provider: provider_block,
    }
    if spec.solution_name:
        engine_config["solution_name"] = spec.solution_name
    return engine_config


def preflight_check(engine_config: dict) -> list[str]:
    """Collect all problems before we touch the browser. Empty list = OK."""
    errors: list[str] = []
    if not engine_config.get("prompts"):
        errors.append("prompts resolved empty — check the prompt registry entry")
    if engine_config.get("prompt_version") is None:
        errors.append(
            "prompt_version is null. It names the prompt the attempt ran "
            "with; set it to a version in tasks_configs/prompts/registry.yaml."
        )
    if not engine_config.get("file_path"):
        errors.append(
            "onedrive_base_path is empty — the engine cannot navigate to "
            "the task folder"
        )
    for raw in engine_config.get("upload_files") or []:
        if not Path(raw).exists():
            errors.append(f"upload file not found: {raw}")
    return errors


def _write_prompts_file(
    run_dir: Path, task_name: str, engine_config: dict, prompt_files: list[str],
    started: datetime,
) -> Path | None:
    """Materialize the per-task prompt payload so the sink can upload it.

    Records the prompt TEXT, not just paths: this JSON is the artifact that
    survives in S3 as evidence of what the agent was actually asked, and a
    path is not evidence once the file changes.
    """
    prompts = engine_config.get("prompts") or []
    if not prompts:
        return None
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_name(task_name)
    ts = started.strftime("%Y%m%d_%H%M%S")
    path = run_dir / f"prompts_{safe_name}_{ts}.json"
    path.write_text(
        json.dumps(
            {
                "prompts": prompts,
                "prompt_version": engine_config.get("prompt_version"),
                "prompt_files": list(prompt_files),
            },
            indent=2,
        )
    )
    return path


def collect_log_files(run_dir: Path) -> list[Path]:
    patterns = ("json_logs/*.json", "general_logs/*.log")
    return [
        p for pattern in patterns for p in sorted(run_dir.glob(pattern)) if p.is_file()
    ]


def read_completion(run_dir: Path) -> dict:
    """The attempt's completion JSON (the engine writes exactly one into
    this attempt's own staging dir). {} when the engine died pre-agent."""
    candidates = sorted((run_dir / "json_logs").glob("completion_*.json"))
    if not candidates:
        return {}
    try:
        return json.loads(candidates[-1].read_text())
    except Exception as e:
        logger.warning(f"Could not parse completion JSON: {e}")
        return {}


def find_solution_file(run_dir: Path, completion: dict) -> Path | None:
    """The engine records the exact downloaded path in the completion JSON
    (solution_file). No mtime/date-folder guessing — if the record is
    missing, the only fallback is the attempt's own solutions/ dir, which
    belongs to this attempt alone."""
    recorded = completion.get("solution_file")
    if recorded:
        p = Path(recorded)
        if p.exists():
            return p
        logger.warning(f"Recorded solution_file does not exist: {p}")
    solutions = run_dir / "solutions"
    if solutions.exists():
        xlsx = sorted(solutions.glob("*.xlsx"))
        if xlsx:
            return xlsx[-1]
    return None


def _kill_engine_tree(proc: subprocess.Popen) -> None:
    """SIGTERM the engine's whole process group, escalate to SIGKILL.

    The engine is launched with start_new_session=True, so killing its
    group reaps anything it spawned — but never the shared Chrome, which
    is detached in its own session and is not a child of the engine."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.error(f"engine pid={proc.pid} survived SIGKILL (?)")


def run_engine(
    engine_config: dict, engine_script: Path, timeout: int | None
) -> int:
    """Run the engine subprocess, streaming its output. Returns its exit
    code, or ENGINE_RC_TIMEOUT if the deadman killed it.

    The deadman is enforced by proc.wait(timeout=...) on the main thread
    while a daemon thread pumps stdout — a wedged engine that stops
    writing but never exits still gets killed. The finally block reaps the
    engine's process group on ANY exit from this function.
    """
    port = (engine_config.get("browser") or {}).get("cdp_port")
    port_tag = f"cdp{port}_" if port else ""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix=f"excel_agents_{port_tag}"
        f"{_sanitize_name(engine_config.get('task_name', 'task'))[:40]}_",
        delete=False,
    ) as f:
        yaml.safe_dump(engine_config, f, default_flow_style=False)
        tmp_path = Path(f.name)
    proc: subprocess.Popen | None = None
    try:
        cmd = [sys.executable, str(engine_script), "--config", str(tmp_path), "--no-hold"]
        logger.info(f"Engine: {' '.join(cmd)}")
        if timeout:
            logger.info(f"Engine deadman: {timeout}s")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert proc.stdout is not None

        def _pump(stream) -> None:
            for line in iter(stream.readline, ""):
                print(line, end="", flush=True)

        pump = threading.Thread(target=_pump, args=(proc.stdout,), daemon=True)
        pump.start()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.error(f"Engine exceeded {timeout}s deadman — killing process group")
            _kill_engine_tree(proc)
            rc = ENGINE_RC_TIMEOUT
        pump.join(timeout=5)
        return rc
    finally:
        if proc is not None:
            _kill_engine_tree(proc)
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _acquire_cdp_lock(port: int):
    """Advisory exclusive lock on the automation Chrome's CDP port. Two
    engines driving one Chrome corrupt BOTH runs. Returns (handle, None)
    on success or (None, holder) when another run holds it."""
    try:
        import fcntl
    except ImportError:
        return None, None
    lock_path = Path(tempfile.gettempdir()) / f"excel_agents_cdp_{port}.lock"
    try:
        fh = open(lock_path, "a+")
    except OSError as e:
        logger.warning(f"CDP lock unavailable ({e}); continuing without it")
        return None, None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown pid"
        fh.close()
        return None, holder
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()} started={datetime.now().isoformat()}")
    fh.flush()
    return fh, None


def _default_deadman(engine_config: dict, provider: str) -> int | None:
    """Ceiling for the engine subprocess: its own per-task budget plus a
    wide grace margin for navigation/download, so the deadman only fires
    on a truly wedged engine."""
    section = engine_config.get(provider, {}) or {}
    try:
        per_task = int(section.get("max_sec_per_task") or 0)
    except (TypeError, ValueError):
        return None
    if per_task <= 0:
        return None
    return per_task + 1800


def _staging_dir(cfg: SimpleNamespace, task_name: str, started: datetime) -> Path:
    stem = _sanitize_name(task_name)
    return (
        Path(cfg.paths.scratch_dir)
        / "attempts"
        / f"{started.strftime('%Y%m%d_%H%M%S')}_{stem}"
    )


def _clear_staging(run_dir: Path, sink) -> None:
    if not getattr(sink, "retains_files", False):
        logger.info(f"Run files kept at {run_dir} (sink records paths only)")
        return
    shutil.rmtree(run_dir, ignore_errors=True)


def _confirm_tasks(specs: list[TaskSpec]) -> bool:
    print(f"\nAbout to run {len(specs)} task(s):")
    for i, spec in enumerate(specs):
        files = ", ".join(p.name for p in spec.upload_files) or "(no files)"
        print(f"  [{i}] {spec.task_name}  —  {files}")
    try:
        answer = input("\nProceed? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(description="excel-agents runner (task-io driven)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Deadman for the engine subprocess, seconds (default: the "
        "provider's max_sec_per_task + 1800).",
    )
    parser.add_argument(
        "--run-config",
        default=None,
        help=(
            "Overlay a run-specific YAML on top of configs.yaml — either a "
            "sparse configs-shaped overlay, or a YAML task file (top-level "
            "task_name/tasks), which forces source.kind='yaml'. Relative "
            "paths resolve from the repo root."
        ),
    )
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the interactive 'proceed?' confirmation.")
    parser.add_argument(
        "--task-id",
        type=int,
        default=None,
        help="Run exactly one task (by DB id); disables skip_already_attempted.",
    )
    parser.add_argument(
        "--skip-if-attempted",
        action="store_true",
        help="Force skip_already_attempted=True (with --task-id, makes an "
        "already-succeeded task a no-op instead of a duplicate attempt).",
    )
    args = parser.parse_args()

    # SIGTERM must unwind through run_engine's finally so the engine tree
    # is reaped rather than orphaned.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))

    run_config_path: Path | None = None
    run_config_is_task_yaml = False
    if args.run_config is not None:
        run_config_path = Path(args.run_config)
        if not run_config_path.is_absolute():
            run_config_path = _REPO_ROOT / run_config_path
        if not run_config_path.exists():
            logger.error(f"--run-config file not found: {run_config_path}")
            return EXIT_CONFIG_ERROR
        with open(run_config_path) as f:
            run_config_data = yaml.safe_load(f) or {}
        if not isinstance(run_config_data, dict):
            logger.error(f"--run-config must be a YAML mapping: {run_config_path}")
            return EXIT_CONFIG_ERROR
        run_config_is_task_yaml = bool(_RUN_CONFIG_TASK_KEYS & set(run_config_data))

    try:
        if run_config_is_task_yaml:
            overlay_data = {
                k: v
                for k, v in run_config_data.items()
                if k not in _RUN_CONFIG_TASK_KEYS
            }
            cfg = load_configs(run_config_data=overlay_data)
        else:
            cfg = load_configs(run_config_path=run_config_path)
    except ConfigError as e:
        logger.error(f"Config load failed:\n{e}")
        return EXIT_CONFIG_ERROR

    if run_config_is_task_yaml:
        cfg.source.kind = "yaml"
        cfg.source.yaml_path = str(run_config_path)
        # A schema pinned in configs.yaml is for the DB source it named —
        # it must not survive into the forced yaml source (registry.py
        # rejects the combination and the error would blame a setting the
        # user never made for this run).
        cfg.source.schema = None

    if args.task_id is not None:
        if cfg.source.kind != "postgres_s3":
            logger.error(
                f"--task-id requires source.kind=postgres_s3 "
                f"(current: {cfg.source.kind!r})."
            )
            return EXIT_CONFIG_ERROR
        filters = getattr(cfg.source, "filters", None)
        if filters is None:
            filters = SimpleNamespace()
            cfg.source.filters = filters
        filters.task_ids = [args.task_id]
        filters.skip_already_attempted = bool(args.skip_if_attempted)
    elif args.skip_if_attempted:
        filters = getattr(cfg.source, "filters", None)
        if filters is None:
            filters = SimpleNamespace()
            cfg.source.filters = filters
        filters.skip_already_attempted = True

    benchmark = (getattr(cfg, "benchmark", None) or "v2").lower()
    logger.info(f"Benchmark: {benchmark}")

    # Identity FIRST, inside the guarded path: an unknown label or a pinned
    # key in the config is a clean config error (exit 2), never a traceback.
    try:
        identity = resolve_agent_identity(cfg)
    except AgentIdentityError as e:
        logger.error(f"Agent identity resolution failed:\n{e}")
        return EXIT_CONFIG_ERROR
    logger.info(
        f"agent identity: model_name={identity.model_name!r} "
        f"provider={identity.provider!r} "
        f"ui_model_label={identity.ui_model_label!r} "
        f"thinking_effort={identity.thinking_effort!r} "
        f"agent_model_type={identity.agent_model_type!r}"
    )

    # Prompt selection: prompt_version picks the files through the registry,
    # so the DB label and the text the agent receives are one decision.
    try:
        prompt_files = resolve_prompt_files(cfg)
        prompt_texts = _load_prompt_texts(prompt_files)
    except (ConfigError, OSError) as e:
        logger.error(f"Prompt selection failed:\n{e}")
        return EXIT_CONFIG_ERROR
    logger.info(describe_prompt_version(cfg, prompt_files))

    # One prompt_version end-to-end: the sink writes cfg.agent.prompt_version;
    # copy the top-level value across (resolve_prompt_files already refused
    # any disagreement between the two keys).
    if getattr(cfg, "prompt_version", None) is not None:
        cfg.agent.prompt_version = cfg.prompt_version

    if "postgres_s3" in (cfg.source.kind, cfg.sink.kind):
        logger.info(f"Database: {describe_database_target(cfg)}")

    engine_script = _REPO_ROOT / "excel_agent" / "engine.py"
    if not engine_script.exists():
        logger.error(f"Engine not found: {engine_script}")
        return EXIT_CONFIG_ERROR

    try:
        source = build_source(cfg)
        sink = build_sink(cfg)
    except ValueError as e:
        logger.error(f"Source/sink build failed:\n{e}")
        return EXIT_CONFIG_ERROR

    runner_cfg = getattr(cfg, "runner", SimpleNamespace())
    max_infra_tries = max(1, int(getattr(runner_cfg, "max_infra_tries", 3) or 3))
    sleep_between = int(getattr(runner_cfg, "sleep_between_retries", 20) or 0)

    succeeded = agent_failed = infra_skipped = 0
    cdp_lock = None
    try:
        specs = list(source.iter_tasks())
        specs = specs[args.start : args.end]
        logger.info(f"Loaded {len(specs)} task(s) from source kind={cfg.source.kind}")
        if not specs:
            logger.warning("No tasks to run (filters excluded everything).")
            return EXIT_NO_TASKS

        # Build + preflight every task BEFORE the confirmation prompt.
        prepared: list[tuple[TaskSpec, dict]] = []
        had_errors = False
        for spec in specs:
            engine_config = build_engine_config(cfg, spec, identity, prompt_texts)
            errors = preflight_check(engine_config)
            if errors:
                had_errors = True
                logger.error(f"Preflight failed for task {spec.task_name!r}:")
                for e in errors:
                    logger.error(f"  - {e}")
            else:
                prepared.append((spec, engine_config))
        if had_errors:
            logger.error(
                "Fix the --run-config or configs.yaml and re-run. "
                "configs.default.yaml lists every available key."
            )
            return EXIT_CONFIG_ERROR

        if not args.dry_run and not args.yes:
            if not _confirm_tasks(specs):
                logger.info("Aborted by user.")
                return EXIT_OK

        if not args.dry_run:
            cdp_port = (
                _ns_to_dict(getattr(cfg, "browser", SimpleNamespace())) or {}
            ).get("cdp_port")
            if cdp_port is not None:
                cdp_lock, holder = _acquire_cdp_lock(int(cdp_port))
                if holder is not None:
                    logger.error(
                        f"Another run already drives Chrome on CDP port "
                        f"{cdp_port} ({holder}). Wait for it or use a "
                        f"different browser.cdp_port."
                    )
                    return EXIT_ENV_BLOCKED

        for i, (spec, engine_config) in enumerate(prepared):
            idx = args.start + i
            logger.info(f"\n{'=' * 60}\nTASK {idx}: {spec.task_name}\n{'=' * 60}")

            if args.dry_run:
                preview = dict(engine_config)
                preview["prompts"] = [
                    f"<{len(t)} chars: {t[:60]!r}...>" for t in preview["prompts"]
                ]
                logger.info("[DRY RUN] engine_config:")
                print(yaml.safe_dump(preview, default_flow_style=False))
                continue

            published = False
            for infra_try in range(1, max_infra_tries + 1):
                started = datetime.now()
                run_dir = _staging_dir(cfg, spec.task_name, started)
                attempt_config = dict(engine_config)
                attempt_config["run_dir"] = str(run_dir)
                attempt_config["attempt_number"] = infra_try - 1
                prompts_file = _write_prompts_file(
                    run_dir, spec.task_name, attempt_config, prompt_files, started
                )

                deadman = (
                    args.timeout
                    if args.timeout is not None
                    else _default_deadman(attempt_config, identity.provider)
                )
                rc = run_engine(attempt_config, engine_script, deadman)
                finished = datetime.now()

                if rc == ENGINE_CONFIG_ERROR:
                    logger.error(
                        "Engine reported a config error — aborting the run "
                        "(every task would fail identically)."
                    )
                    return EXIT_CONFIG_ERROR

                if rc in (ENGINE_SUCCESS, ENGINE_AGENT_FAILURE):
                    completion = read_completion(run_dir)
                    task_status = ""
                    for task_entry in completion.get("tasks", []) or []:
                        task_status = task_entry.get("task_status") or task_status
                    if rc == ENGINE_SUCCESS:
                        status = "success"
                        succeeded += 1
                    elif task_status == "timeout":
                        status = "timeout"
                        agent_failed += 1
                    else:
                        status = "failed"
                        agent_failed += 1

                    solution_file = find_solution_file(run_dir, completion)
                    extra: dict = {
                        "return_code": rc,
                        "task_metadata": dict(spec.metadata or {}),
                        "extra_configs": {
                            "cdp_port": (attempt_config.get("browser") or {}).get(
                                "cdp_port"
                            ),
                            "infra_tries": infra_try,
                            "engine_task_status": task_status or None,
                        },
                    }
                    if status != "success" and task_status:
                        extra["failure_reason"] = task_status

                    result = AttemptResult(
                        task_id=spec.task_id,
                        task_name=spec.task_name,
                        agent_model_name=identity.model_name,
                        prompt_version=cfg.agent.prompt_version,
                        status=status,
                        solution_file=solution_file,
                        log_files=collect_log_files(run_dir),
                        started_at=started.isoformat(),
                        finished_at=finished.isoformat(),
                        duration_seconds=round(
                            (finished - started).total_seconds(), 2
                        ),
                        prompt_files=[prompts_file] if prompts_file else [],
                        extra=extra,
                    )
                    sink.publish(result)
                    _clear_staging(run_dir, sink)
                    published = True
                    break

                # Infra failure (rc 3, deadman 124, or anything else):
                # nothing recorded, no trial burned. Keep the staging dir of
                # the FAILED try for diagnosis only if the sink wouldn't
                # (it's scratch; the next try gets a fresh dir).
                logger.warning(
                    f"Infra failure (rc={rc}) on try {infra_try}/"
                    f"{max_infra_tries} — nothing recorded"
                )
                shutil.rmtree(run_dir, ignore_errors=True)
                if infra_try < max_infra_tries and sleep_between:
                    logger.info(f"Sleeping {sleep_between}s before retry...")
                    time.sleep(sleep_between)

            if not published:
                infra_skipped += 1
                logger.error(
                    f"Task {spec.task_name!r}: {max_infra_tries} infra "
                    f"failures — skipped for this invocation (still "
                    f"eligible for future runs; no attempt recorded)"
                )

        logger.info(
            f"\nDone. succeeded={succeeded} agent_failed={agent_failed} "
            f"infra_skipped={infra_skipped}"
        )
    finally:
        if cdp_lock is not None:
            try:
                cdp_lock.close()
            except Exception:
                pass
        source.close()
        sink.close()

    return (
        EXIT_OK if (agent_failed == 0 and infra_skipped == 0) else EXIT_TASK_FAILED
    )


if __name__ == "__main__":
    sys.exit(main())
