"""task-io-driven runner for gui-agents.

Reads configs from infra/configs/, builds a TaskSource + AttemptSink, and
drives the existing claude_web_engine.py subprocess once per task.

Config layering (later wins), all of it project-wide — there is no
per-task override layer:

    1. infra/configs/configs.default.yaml   (schema + defaults)
    2. infra/configs/configs.yaml           (optional; the pushed run
       profile on EC2 boxes, absent on a laptop)
    3. --run-config <file>                  (the per-experiment overlay)

Credentials are NOT part of that stack. The database url and AWS keys come
from the monorepo config at <repo>/config/config.yaml, with the url picked
by `benchmark` (v1 -> database.v1_url, v2 -> database.v2_url); boxes, which
never see that file, fall back to the env vars in /etc/gui-agents/secrets.env.
See task_io/registry.py for the full precedence.

Per task, the engine config is then built by selecting the active provider
block from the merged cfg and assembling the dict the engine expects.

Usage (from gui-agents-master/):
    python -m infra.run                       # real run, uses configs.yaml if present
    python -m infra.run --dry-run             # print merged engine configs
    python -m infra.run --start 0 --end 1     # slice tasks

Exit-code contract (consumed by infra/worker/worker_loop.py — keep in sync):
    0   ran >=1 task and every attempt succeeded (also: --dry-run, or the
        user declined the interactive confirmation)
    1   ran >=1 task and >=1 attempt failed
    2   config / preflight / CLI error — nothing was attempted
    3   the source yielded no tasks (filters excluded everything, empty
        slice, or --skip-if-attempted matched an existing attempt) —
        nothing was attempted.
    4   environment gate blocked the run before any task started (CDP
        lock held by another run, or --auth-precheck failed)
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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.configs import (  # noqa: E402
    ConfigError,
    describe_prompt_version,
    load_configs,
    resolve_agent_identity,
    resolve_prompt_files,
)
from task_io import (  # noqa: E402
    AttemptResult,
    TaskSpec,
    build_sink,
    build_source,
    describe_database_target,
)
from claude_web_agent.claude_web_engine import (  # noqa: E402
    _sanitize_name,
    resolve_prompts,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("infra.run")

PROVIDER_AGENT_TYPE = {"claude": "claude_web", "chatgpt": "chatgpt_web"}

# Exit codes — see the module docstring for the full contract.
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_NO_TASKS = 3
EXIT_ENV_BLOCKED = 4

# run_engine's sentinel for "engine subprocess exceeded the deadman and was
# killed" — matches coreutils timeout(1) so log readers recognize it.
ENGINE_RC_TIMEOUT = 124

# If a --run-config file has any of these at top level, treat it as a YAML
# task file (hand it to YamlTaskSource) instead of a project-wide overlay.
_RUN_CONFIG_TASK_KEYS = {
    "task_name",
    "upload_files",
    "files_to_upload",
    "solution_name",
    "skip",
    "task_source",
    "tasks",
}


def _ns_to_dict(obj):
    """Recursively convert SimpleNamespace (from load_configs) to plain dicts."""
    if isinstance(obj, SimpleNamespace):
        return {k: _ns_to_dict(v) for k, v in vars(obj).items()}
    if isinstance(obj, list):
        return [_ns_to_dict(v) for v in obj]
    return obj


def build_engine_config(cfg: SimpleNamespace, spec: TaskSpec) -> dict:
    """Assemble the full engine-input dict for one task.

    All overrides (defaults + configs.yaml + --run-config) are already
    baked into `cfg` by the loader. This function only projects the
    active provider block + task fields into the shape the engine expects:
        {agent_type, prompts, prompt_version, local_files_base,
         task_name, task_id, task_source, upload_files, solution_name,
         <provider>_web: {...}}

    Prompt selection also lands here rather than only in main(): this is the
    function that decides what the engine receives, so a caller that skips
    main() (tests, any future entrypoint) must not silently produce a config
    with no prompts. main() resolves first so it can log the choice once per
    run, in which case cfg.prompts_file is already set and this is a no-op.
    """
    if not getattr(cfg, "prompts_file", None):
        cfg.prompts_file = resolve_prompt_files(cfg)
    base = _ns_to_dict(cfg)

    provider = cfg.provider.kind
    agent_type = PROVIDER_AGENT_TYPE.get(provider, "claude_web")
    provider_block_key = f"{provider}_web"

    engine_config: dict = {
        "agent_type": agent_type,
        "prompts": list(base.get("prompts") or []),
        "prompt_version": base.get("prompt_version"),
    }

    # prompts_file (single source of truth for the long benchmark prompts)
    # is passed through; the engine expands it into `prompts`.
    if base.get("prompts_file"):
        engine_config["prompts_file"] = base["prompts_file"]

    engine_config |= {
        "task_name": spec.task_name,
        "task_id": spec.task_id,
        "upload_files": [str(p) for p in spec.upload_files],
        provider_block_key: copy.deepcopy(base.get(provider_block_key, {}) or {}),
    }

    local_files_base = base.get("local_files_base")
    if local_files_base:
        engine_config["local_files_base"] = local_files_base

    if spec.solution_name:
        engine_config["solution_name"] = spec.solution_name

    task_source = (
        spec.metadata.get("task_source") if isinstance(spec.metadata, dict) else None
    )
    if task_source:
        engine_config["task_source"] = task_source

    return engine_config


def _resolve_upload_path(raw: str, local_files_base: str | None) -> Path:
    """Mirror the engine's resolution: relative paths resolve against
    local_files_base if set, else stay relative to CWD."""
    p = Path(raw)
    if not p.is_absolute() and local_files_base:
        p = (Path(local_files_base) / p).resolve()
    return p


def _preflight_provider_v1(
    provider: str, section: dict, errors: list[str]
) -> None:
    """Benchmark-v1 provider checks: the UI axes the V1 wave pins (Claude
    chat/cowork mode + effort; ChatGPT chat/work mode + intelligence or
    effort+speed) all bifurcate the DB identity, so invalid values must
    fail before the browser is touched."""
    _CLAUDE_MODELS = (
        "fable_5", "sonnet_5", "haiku_4_5", "opus_4_8",
        "opus_4_7", "opus_4_6", "opus_3", "sonnet_4_6",
    )
    _EFFORTS = ("low", "medium", "high", "xhigh", "max")
    _CHATGPT_MODELS = ("gpt_5_6_sol", "gpt_5_5", "gpt_5_4", "gpt_5_3", "o3")
    _CHATGPT_WORK_MODELS = (
        "gpt_5_6_sol", "gpt_5_6_terra", "gpt_5_6_luna", "gpt_5_5",
    )
    _INTELLIGENCE = ("instant", "medium", "high", "xhigh", "pro")
    _WORK_EFFORTS = ("light", "medium", "high", "xhigh", "max", "ultra")
    _WORK_SPEEDS = ("standard", "fast")
    if provider == "claude":
        mode = (section.get("mode") or "chat").lower()
        if mode not in ("chat", "cowork"):
            errors.append(
                f"claude_web.mode={mode!r} is not 'chat' or 'cowork'."
            )
        approval = section.get("cowork_approval")
        if approval is not None and approval not in ("manual", "auto", "skip"):
            errors.append(
                f"claude_web.cowork_approval={approval!r} is not one of "
                "manual, auto, skip (or null = auto)."
            )
        if section.get("model") is None:
            errors.append(
                "claude_web.model is null. The agent tolerates null (keeps "
                "the session default), but benchmark runs must pin the model "
                f"for the DB identity. Set one of: {', '.join(_CLAUDE_MODELS)}."
            )
        effort = section.get("effort")
        if effort is not None and effort not in _EFFORTS:
            errors.append(
                f"claude_web.effort={effort!r} is not one of "
                f"{', '.join(_EFFORTS)} (or null)."
            )
    elif provider == "chatgpt":
        gmode = (section.get("mode") or "chat").lower()
        if gmode not in ("chat", "work"):
            errors.append(f"chatgpt_web.mode={gmode!r} is not 'chat' or 'work'.")
        if gmode == "work":
            model = section.get("model")
            if model is not None and model not in _CHATGPT_WORK_MODELS:
                errors.append(
                    f"chatgpt_web.model={model!r} is not a work-mode model "
                    f"({', '.join(_CHATGPT_WORK_MODELS)}) or null."
                )
            w_effort = section.get("effort")
            if w_effort is not None and w_effort not in _WORK_EFFORTS:
                errors.append(
                    f"chatgpt_web.effort={w_effort!r} is not one of "
                    f"{', '.join(_WORK_EFFORTS)} (or null)."
                )
            w_speed = section.get("speed")
            if w_speed is not None and w_speed not in _WORK_SPEEDS:
                errors.append(
                    f"chatgpt_web.speed={w_speed!r} is not one of "
                    f"{', '.join(_WORK_SPEEDS)} (or null = standard)."
                )
            if section.get("intelligence") is not None:
                errors.append(
                    "chatgpt_web.intelligence is set but mode=work — the "
                    "intelligence axis exists only in chat mode. Use "
                    "chatgpt_web.effort (+ speed) for work mode."
                )
        else:
            intel = section.get("intelligence")
            if intel is not None and intel not in _INTELLIGENCE:
                errors.append(
                    f"chatgpt_web.intelligence={intel!r} is not one of "
                    f"{', '.join(_INTELLIGENCE)} (or null)."
                )
            # Legacy one-axis values stay valid: identity has entries for
            # them and the agent routes them to the intelligence axis (with
            # a warning). Only genuinely unknown values are errors.
            _LEGACY_CHATGPT_MODELS = ("instant", "thinking", "pro")
            model = section.get("model")
            if (
                model is not None
                and model not in _CHATGPT_MODELS
                and model not in _LEGACY_CHATGPT_MODELS
            ):
                errors.append(
                    f"chatgpt_web.model={model!r} is not one of "
                    f"{', '.join(_CHATGPT_MODELS)} "
                    f"(legacy: {', '.join(_LEGACY_CHATGPT_MODELS)}) or null."
                )


def _preflight_provider_v2(
    provider: str, section: dict, errors: list[str]
) -> None:
    """Benchmark-v2 provider checks: identity bifurcates on model only."""
    if provider == "claude":
        if section.get("model") is None:
            errors.append(
                "claude_web.model is null — the agent calls .lower() on it and crashes. "
                "Set claude_web.model to 'sonnet_4_6' / 'opus_4_6' / 'opus_4_8' / "
                "'haiku_4_5' / 'fable_5'."
            )


def preflight_check(
    engine_config: dict, provider: str, benchmark: str = "v2"
) -> list[str]:
    """Collect all problems before we touch the browser. Empty list = OK."""
    errors: list[str] = []
    section_key = f"{provider}_web"
    section = engine_config.get(section_key, {}) or {}

    # Provider-specific contract checks, benchmark-dependent: the v1 wave
    # pins UI axes (mode/effort/intelligence) that v2 doesn't use.
    if (benchmark or "v2").lower() == "v1":
        _preflight_provider_v1(provider, section, errors)
    else:
        _preflight_provider_v2(provider, section, errors)

    # chatgpt_web.project_id is optional: null/empty falls back to the
    # chatgpt.com homepage (no project scope), mirroring the claude_web
    # project_id fallback. The agent logs a warning in that case.
    # project_slug is likewise optional — some ChatGPT project URLs have no
    # slug (e.g. https://chatgpt.com/g/g-p-{id}/project with no -{slug}).

    # prompts_file must exist and be non-empty — a benchmark run with the
    # wrong (or empty) prompt is worse than one that never starts.
    pf = engine_config.get("prompts_file")
    if pf:
        repo_root = Path(__file__).resolve().parent.parent
        for raw in [pf] if isinstance(pf, str) else pf:
            p = Path(raw)
            if not p.is_absolute():
                cand = repo_root / p
                p = cand if cand.exists() else Path.cwd() / p
            if not p.exists():
                errors.append(f"prompts_file not found: {raw}")
            elif p.stat().st_size == 0:
                errors.append(f"prompts_file is empty: {raw}")
    elif not engine_config.get("prompts"):
        errors.append(
            "No prompts configured — set prompts_file (preferred) or prompts."
        )

    # Upload files must exist on disk, resolved the same way the engine will.
    upload_files = engine_config.get("upload_files") or []
    local_files_base = engine_config.get("local_files_base")
    for raw in upload_files:
        resolved = _resolve_upload_path(str(raw), local_files_base)
        if not resolved.exists():
            if local_files_base and not Path(raw).is_absolute():
                hint = (
                    f" (resolved from local_files_base={local_files_base!r} + "
                    f"{raw!r})"
                )
            elif not Path(raw).is_absolute():
                hint = (
                    " (relative path — set local_files_base in configs.yaml or "
                    "make the path absolute)"
                )
            else:
                hint = ""
            errors.append(f"upload file not found: {resolved}{hint}")

    return errors


def collect_log_files(run_dir: Path) -> list[Path]:
    """Every log the attempt produced, in upload order.

    json_logs/ holds one completion_*.json per agent attempt (the engine
    retries internally); logs/ holds the chat transcript and the runtime
    log. The staging directory belongs to this attempt alone, so everything
    under it is in scope.
    """
    patterns = ("json_logs/*.json", "logs/conversations/*.json", "logs/*.log")
    return [
        p for pattern in patterns for p in sorted(run_dir.glob(pattern)) if p.is_file()
    ]


def _write_prompts_file(
    run_dir: Path, task_name: str, engine_config: dict, started: datetime
) -> Path | None:
    """Materialize the per-task prompt payload so the sink can upload it.

    Records the prompt TEXT, not the file paths: this JSON is the only
    artifact that survives in S3 as evidence of what the agent was actually
    asked, and a path is not evidence once the file changes.

    `prompts` is empty in engine_config whenever the run uses `prompts_file`
    (the normal path — the engine expands it in the subprocess, after this
    record is written), so expand it here through the engine's own resolver
    rather than reading the key. That resolver is also the one the engine
    will use, so the recorded text is what the agent receives, not a second
    guess at it.

    Returns None only if the run genuinely has no prompts to log — the sink
    treats a missing path as "no prompt_files to record" rather than a
    failure."""
    prompts = engine_config.get("prompts") or []
    if not prompts and engine_config.get("prompts_file"):
        try:
            prompts = resolve_prompts(dict(engine_config)).get("prompts") or []
        except (FileNotFoundError, OSError) as e:
            # Preflight already checked existence, so this is close to
            # unreachable — but losing the record must not lose the run.
            logger.warning(f"Could not record prompt text: {e}")
            prompts = []
    if not prompts:
        return None
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_name)
    ts = started.strftime("%Y%m%d_%H%M%S")
    path = run_dir / f"prompts_{safe_name}_{ts}.json"
    pf = engine_config.get("prompts_file")
    payload = {
        "prompts": prompts,
        "prompt_version": engine_config.get("prompt_version"),
        "prompts_file": [pf] if isinstance(pf, str) else list(pf or []),
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def find_solution_file(
    run_dir: Path, task_name: str, solution_name: str | None, after: datetime
) -> Path | None:
    solutions = run_dir / "solutions"
    if not solutions.exists():
        return None
    # Must match rename_solution_file's sanitizer; otherwise task names with
    # stripped chars (e.g. '&') fail to match the on-disk filename.
    needle = _sanitize_name(solution_name or task_name).lower()
    matches: list[tuple[float, Path]] = []
    for p in solutions.glob("*.xlsx"):
        if p.stat().st_mtime < after.timestamp():
            continue
        if needle in p.name.lower():
            matches.append((p.stat().st_mtime, p))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


# Post-run output quality gate. Heuristic floor to catch obviously-degraded
# workbooks (e.g. a tiny stub produced when a ChatGPT "content failed to load"
# disruption truncated the model). Tunable.
QUALITY_MIN_BYTES = 12000
QUALITY_MIN_SHEETS = 3


def check_output_quality(solution_file: Path | None) -> tuple[bool, str]:
    """Return (ok, reason). ok=False flags a suspected-degraded output.

    Runs in infra.run AFTER the engine subprocess returns — i.e. OUTSIDE the
    engine's own retry loop — so it only records a verdict and never triggers
    a re-run. Fails OPEN on inspection errors (can't break the pipeline over a
    false alarm) except a missing/unopenable workbook, which IS a failure.
    """
    if solution_file is None:
        return False, "no solution workbook was produced"
    try:
        size = Path(solution_file).stat().st_size
    except OSError as e:
        return False, f"solution file not accessible: {e}"
    try:
        import openpyxl

        wb = openpyxl.load_workbook(solution_file, read_only=True)
        n_sheets = len(wb.sheetnames)
        wb.close()
    except ImportError:
        logger.warning("openpyxl unavailable; skipping output quality gate")
        return True, ""
    except Exception as e:
        return False, (
            f"solution workbook could not be opened ({type(e).__name__}); "
            f"likely corrupt/partial"
        )
    if size < QUALITY_MIN_BYTES or n_sheets < QUALITY_MIN_SHEETS:
        return False, (
            f"output below quality floor: {size} bytes / {n_sheets} sheet(s) "
            f"(min {QUALITY_MIN_BYTES} bytes, {QUALITY_MIN_SHEETS} sheets) — "
            f"suspected content-load disruption"
        )
    return True, ""


def _kill_engine_tree(proc: subprocess.Popen) -> None:
    """SIGTERM the engine's whole process group, escalate to SIGKILL.

    The engine is launched with start_new_session=True, so killing its group
    reaps anything it spawned without touching run.py's own group (and,
    critically, without signalling the shared Chrome — that lives in a
    separate service/session and is never a child of the engine)."""
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


def run_engine(engine_config: dict, engine_script: Path, timeout: int | None,
               cdp_port: int | None = None) -> int:
    """Run the engine subprocess, streaming its output. Returns its exit
    code, or ENGINE_RC_TIMEOUT if the deadman killed it.

    The deadman is enforced by proc.wait(timeout=...) on the main thread
    while a daemon thread pumps stdout. The previous implementation read
    stdout to EOF on the main thread BEFORE calling wait(timeout=...), so a
    wedged engine that stopped writing but never exited (e.g. a hung
    Playwright await after the renderer died) blocked forever and the
    timeout never even started — the deadman existed but could not fire.

    The finally-block guarantees the engine's process group is reaped on
    ANY exit from this function (deadman, KeyboardInterrupt, SIGTERM via
    the SystemExit handler in main, unexpected exception) — run.py never
    leaves an orphaned engine driving the shared Chrome.
    """
    # The CDP port is embedded in the temp-config filename so external
    # supervisors (e.g. infra/overnight) can pgrep-scope the engine child to
    # ONE browser instance — parallel runs on distinct ports must never sweep
    # or kill each other's engine.
    port_tag = f"cdp{cdp_port}_" if cdp_port is not None else ""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix=f"gui_agents_{port_tag}{engine_config.get('task_name', 'task')}_",
        delete=False,
    ) as f:
        yaml.safe_dump(engine_config, f, default_flow_style=False)
        tmp_path = Path(f.name)
    proc: subprocess.Popen | None = None
    try:
        cmd = [
            sys.executable,
            str(engine_script),
            "--config",
            str(tmp_path),
            "--no-hold",
        ]
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
            logger.error(
                f"Engine exceeded {timeout}s deadman — killing process group"
            )
            _kill_engine_tree(proc)
            rc = ENGINE_RC_TIMEOUT
        pump.join(timeout=5)  # flush whatever output remains
        return rc
    finally:
        if proc is not None:
            _kill_engine_tree(proc)
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _resolve_cdp_port(cfg: SimpleNamespace, provider: str) -> int | None:
    """Active provider block's browser.cdp_port, or None if not configured."""
    try:
        return int(getattr(cfg, f"{provider}_web").browser.cdp_port)
    except (AttributeError, TypeError, ValueError):
        return None


def _acquire_cdp_lock(port: int):
    """Advisory exclusive lock on the shared Chrome's CDP port.

    Two engines driving one Chrome corrupt BOTH runs (interleaved clicks,
    stolen focus), so a second run.py targeting the same port must fail
    fast instead of starting. flock releases automatically when this
    process exits — including a SIGKILL — so a dead run can never wedge
    the port shut.

    Returns (lock_file_handle, None) on success — caller must keep the
    handle alive for the duration of the run — or (None, holder_info) if
    another process holds the lock. Fails OPEN (None, None handled by
    caller as acquired) is deliberately NOT used: no fcntl means no lock
    semantics anywhere, so we just skip locking on such platforms.
    """
    try:
        import fcntl
    except ImportError:
        return None, None  # non-POSIX: skip locking entirely
    lock_path = Path(tempfile.gettempdir()) / f"gui_agents_cdp_{port}.lock"
    try:
        fh = open(lock_path, "a+")
    except OSError as e:
        # E.g. the file exists owned by another user (box runs as root,
        # manual debug run as ubuntu). Locking is advisory protection —
        # skip it rather than block a legitimate run.
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


def _auth_precheck(cfg: SimpleNamespace, provider: str) -> tuple[bool, str]:
    """Verify the browser session is logged in (and, for chatgpt, on the
    expected plan) before burning a task on a dead session. Reuses the
    worker box's probe logic — same CDP-attach, same session checks."""
    port = _resolve_cdp_port(cfg, provider)
    if port is None:
        return False, f"no browser.cdp_port configured for provider {provider!r}"
    try:
        from infra.worker.auth_probe import _run_probe

        ok, reason, extra = _run_probe(provider, port)
    except Exception as e:
        return False, f"probe error: {type(e).__name__}: {e}"
    if ok:
        who = extra.get("email") or ""
        plan = extra.get("plan") or ""
        detail = f" ({who}{f', plan={plan}' if plan else ''})" if who else ""
        return True, f"session ok{detail}"
    return False, reason or "not logged in"


def _default_deadman(engine_config: dict, provider: str) -> int | None:
    """Conservative ceiling for the engine subprocess when --timeout is not
    given: every legitimate run — all retry attempts at their own per-task
    budget — fits under it with a wide grace margin, so it only fires on a
    truly wedged engine, whose internal guard cannot preempt a hung await.
    Returns None if the config keys aren't available, leaving the subprocess
    unlimited."""
    section = engine_config.get(f"{provider}_web", {}) or {}
    try:
        per_task = int(section.get("max_sec_per_task") or 0)
        retry = section.get("retry", {}) or {}
        attempts = int(retry.get("max_total_attempts") or 0)
    except (TypeError, ValueError):
        return None
    if per_task <= 0 or attempts <= 0:
        return None
    return per_task * attempts + 1800


def _staging_dir(cfg: SimpleNamespace, task_name: str, started: datetime) -> Path:
    """The working directory for one attempt.

    Everything the run produces lands here — the engine's solutions/,
    json_logs/ and logs/, plus the prompts JSON this module writes. The sink
    uploads the lot and _clear_staging removes the directory afterwards, so
    the only lasting copy is the one under paths.output_dir mirroring S3.
    """
    stem = _sanitize_name(task_name)
    return (
        Path(cfg.paths.scratch_dir)
        / "attempts"
        / f"{started.strftime('%Y%m%d_%H%M%S')}_{stem}"
    )


def _clear_staging(run_dir: Path, sink) -> None:
    """Delete the attempt's staging directory once the sink has the files.

    Sinks that only record paths (the `local` sink) keep it — removing it
    would destroy the only copy of the workbook.
    """
    if not getattr(sink, "retains_files", False):
        logger.info(f"Run files kept at {run_dir} (sink records paths only)")
        return
    shutil.rmtree(run_dir, ignore_errors=True)


def _confirm_tasks(specs: list[TaskSpec]) -> bool:
    """Print the loaded task list and ask the user to confirm."""
    print(f"\nAbout to run {len(specs)} task(s):")
    for i, spec in enumerate(specs):
        files = ", ".join(p.name for p in spec.upload_files) or "(no files)"
        print(f"  [{i}] {spec.task_name}  —  {files}")
    try:
        answer = input("\nProceed? [y/N]: ").strip().lower()
    except EOFError:
        # Non-interactive stdin — treat as "no" unless --yes was passed.
        return False
    return answer in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(description="gui-agents runner (task-io driven)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument(
        "--run-config",
        default=None,
        help=(
            "Overlay a run-specific YAML on top of configs.yaml. The file is "
            "either (a) a sparse configs.yaml-shaped overlay (source/filters/"
            "provider/prompts/…) merged as a 3rd config layer, or (b) a "
            "YAML task file (top-level task_name/upload_files/tasks), which "
            "forces source.kind='yaml' and is read by YamlTaskSource. "
            "Relative paths resolve from the repo root."
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the interactive 'proceed?' confirmation.",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        default=None,
        help=(
            "Run exactly one task (by DB id). Pins source.filters.task_ids "
            "to this value and disables skip_already_attempted so the run "
            "proceeds even if an earlier attempt exists. Used by the "
            "worker loop to execute one queued task per invocation."
        ),
    )
    parser.add_argument(
        "--skip-if-attempted",
        action="store_true",
        help=(
            "Force source.filters.skip_already_attempted=True — with "
            "--task-id this overrides its default re-run behavior, so a "
            "task that already has a successful attempt becomes a no-op "
            "(exit 3) instead of a duplicate attempt."
        ),
    )
    parser.add_argument(
        "--auth-precheck",
        action="store_true",
        help=(
            "Before running, probe the provider session over CDP (login + "
            "plan check, same probe the worker boxes use). Exit 4 without "
            "touching any task if the session is dead."
        ),
    )
    args = parser.parse_args()

    # SIGTERM (systemctl stop, orchestrator kill) must run our finally
    # blocks — Python's default handler exits immediately, which would
    # orphan the engine subprocess (it lives in its own process group, so
    # the terminal's signals do not reach it). SystemExit unwinds through
    # run_engine's finally, reaping the engine tree.
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
            logger.error(
                f"--run-config must be a YAML mapping at top level: "
                f"{run_config_path}"
            )
            return EXIT_CONFIG_ERROR
        run_config_is_task_yaml = bool(_RUN_CONFIG_TASK_KEYS & set(run_config_data))

    try:
        if run_config_is_task_yaml:
            # Task-shaped file: strip reserved task fields (task_name,
            # upload_files, …) and overlay the remaining keys as a project-
            # wide layer. YamlTaskSource then reads the same file for the
            # task definition. There's no separate per-task override layer
            # — everything in this file is run-scoped.
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

    if args.task_id is not None:
        if cfg.source.kind != "postgres_s3":
            logger.error(
                f"--task-id requires source.kind=postgres_s3 (current: "
                f"{cfg.source.kind!r}). Use a run-config with "
                f"source.kind=postgres_s3 or drop --task-id."
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

    provider = cfg.provider.kind
    benchmark = (getattr(cfg, "benchmark", None) or "v2").lower()
    logger.info(f"Benchmark: {benchmark}")

    # Prompt selection. An explicit prompts_file in the run config wins and
    # skips the registry; otherwise prompt_version picks the files, so the
    # DB label and the text the agent receives are the same decision. See
    # tasks_configs/prompts/registry.yaml.
    if getattr(cfg, "prompts_file", None):
        logger.info(
            f"prompts_file set explicitly ({cfg.prompts_file}) — prompt "
            f"registry bypassed; prompt_version is a label only for this run."
        )
    else:
        try:
            cfg.prompts_file = resolve_prompt_files(cfg)
        except ConfigError as e:
            logger.error(f"Prompt selection failed:\n{e}")
            return EXIT_CONFIG_ERROR
        logger.info(describe_prompt_version(cfg, cfg.prompts_file))

    # The sink writes cfg.agent.prompt_version to task_attempts. Copy the
    # top-level value across so the recorded version is by construction the
    # one that selected the prompts, rather than a second key that can drift
    # from it. resolve_prompt_files already refused any disagreement.
    if getattr(cfg, "prompt_version", None) is not None:
        cfg.agent.prompt_version = cfg.prompt_version

    # Which database this run reads/writes, and which layer supplied it.
    # Logged BEFORE the build so a credential failure still shows what was
    # attempted. Never prints the password — see describe_database_target.
    if "postgres_s3" in (cfg.source.kind, cfg.sink.kind):
        logger.info(f"Database: {describe_database_target(cfg)}")

    engine_script = _REPO_ROOT / "claude_web_agent" / "claude_web_engine.py"
    if not engine_script.exists():
        logger.error(f"Engine not found: {engine_script}")
        return EXIT_CONFIG_ERROR

    try:
        source = build_source(cfg)
        sink = build_sink(cfg)
    except ValueError as e:
        # Build failures here are user-facing: empty required fields
        # (database.url, aws.*), unknown source/sink kinds, etc.
        # Swallow the traceback and log the message the class raised.
        logger.error(f"Source/sink build failed:\n{e}")
        return EXIT_CONFIG_ERROR

    identity = resolve_agent_identity(cfg)
    logger.info(
        f"agent identity: model_name={identity.model_name!r} "
        f"agent_folder={identity.agent_folder!r} "
        f"agent_model_type={identity.agent_model_type!r}"
    )

    succeeded = failed = 0
    cdp_lock = None
    try:
        specs = list(source.iter_tasks())
        specs = specs[args.start : args.end]
        logger.info(f"Loaded {len(specs)} task(s) from source kind={cfg.source.kind}")

        if not specs:
            logger.warning(
                "No tasks to run (source filters excluded everything, or "
                "the slice is empty)."
            )
            return EXIT_NO_TASKS

        # Build + preflight every task BEFORE the user-confirmation prompt.
        # If any task has null configs or missing files, abort here so the
        # user never sees "About to run" for a run that's guaranteed to fail.
        prepared: list[tuple[TaskSpec, dict]] = []
        had_errors = False
        for spec in specs:
            engine_config = build_engine_config(cfg, spec)
            errors = preflight_check(engine_config, provider, benchmark)
            if errors:
                had_errors = True
                logger.error(
                    f"Preflight failed for task {spec.task_name!r} "
                    f"(provider={provider!r}):"
                )
                for e in errors:
                    logger.error(f"  - {e}")
            else:
                prepared.append((spec, engine_config))
        if had_errors:
            logger.error(
                "Fix the --run-config or the task YAML and re-run. "
                "configs.default.yaml lists every available key."
            )
            return EXIT_CONFIG_ERROR

        if not args.dry_run and not args.yes:
            if not _confirm_tasks(specs):
                logger.info("Aborted by user.")
                return 0

        # Environment gates — only for real runs (dry-run never touches the
        # browser). Both fail BEFORE any task starts, with a distinct exit
        # code, so callers can tell "environment blocked" from "task failed".
        if not args.dry_run:
            cdp_port = _resolve_cdp_port(cfg, provider)
            if cdp_port is not None:
                cdp_lock, holder = _acquire_cdp_lock(cdp_port)
                if holder is not None:
                    logger.error(
                        f"Another run already drives Chrome on CDP port "
                        f"{cdp_port} ({holder}) — two engines on one browser "
                        f"corrupt both runs. Wait for it or use a different "
                        f"browser/port."
                    )
                    return EXIT_ENV_BLOCKED
            if args.auth_precheck:
                ok, detail = _auth_precheck(cfg, provider)
                if not ok:
                    logger.error(f"Auth precheck failed: {detail}")
                    return EXIT_ENV_BLOCKED
                logger.info(f"Auth precheck: {detail}")

        for i, (spec, engine_config) in enumerate(prepared):
            idx = args.start + i
            logger.info(f"\n{'=' * 60}\nTASK {idx}: {spec.task_name}\n{'=' * 60}")

            if args.dry_run:
                logger.info("[DRY RUN] engine_config:")
                print(yaml.safe_dump(engine_config, default_flow_style=False))
                continue

            started = datetime.now()
            run_dir = _staging_dir(cfg, spec.task_name, started)
            engine_config["run_dir"] = str(run_dir)
            prompts_file = _write_prompts_file(
                run_dir, spec.task_name, engine_config, started
            )

            # Deadman: --timeout wins; otherwise derive a conservative
            # ceiling from the provider's own retry budget (None = the old
            # unlimited behavior if those keys aren't configured).
            deadman = (
                args.timeout
                if args.timeout is not None
                else _default_deadman(engine_config, provider)
            )
            rc = run_engine(engine_config, engine_script, deadman, cdp_port)
            finished = datetime.now()
            solution_file = find_solution_file(
                run_dir, spec.task_name, spec.solution_name, started
            )
            if rc == 0:
                status = "success"
            elif rc == ENGINE_RC_TIMEOUT:
                status = "timeout"
            else:
                status = "failed"

            # Post-engine output quality gate. Runs outside the engine's retry
            # loop, so it only RECORDS the verdict — it never re-runs. A degraded
            # output is marked failed-with-reason (no retry, no deprecation);
            # the stub still uploads to S3 for inspection.
            quality_reason: str | None = None
            if status == "success":
                ok, reason = check_output_quality(solution_file)
                if not ok:
                    status = "failed"
                    quality_reason = reason
                    logger.warning(f"Output quality gate FAILED: {reason}")

            if status == "success":
                succeeded += 1
            else:
                failed += 1

            extra: dict = {
                "return_code": rc,
                "task_metadata": dict(spec.metadata or {}),
            }
            if quality_reason:
                extra["failure_reason"] = quality_reason

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
                duration_seconds=round((finished - started).total_seconds(), 2),
                prompt_files=[prompts_file] if prompts_file else [],
                extra=extra,
            )
            sink.publish(result)
            _clear_staging(run_dir, sink)

        logger.info(f"\nDone. succeeded={succeeded} failed={failed}")
    finally:
        if cdp_lock is not None:
            try:
                cdp_lock.close()  # closing the fd releases the flock
            except Exception:
                pass
        source.close()
        sink.close()

    return EXIT_OK if failed == 0 else EXIT_TASK_FAILED


if __name__ == "__main__":
    sys.exit(main())
