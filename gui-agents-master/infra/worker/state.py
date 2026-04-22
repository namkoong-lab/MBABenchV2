"""Box-local state.json — the source of truth for what this worker is doing.

Lives at /var/lib/gui-agents/state.json on each EC2 box. Read/written only
on the box; the laptop reads it over SSH via `gui-agents-queue show`.

All mutations go through `locked_state()` which holds an exclusive
fcntl.flock on the file for the duration of the read-modify-write cycle.
Both the worker loop and the ssh-driven `gui-agents-queue` CLI share this
helper, so every writer is serialized.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATE_DIR = Path("/var/lib/gui-agents")
STATE_PATH = STATE_DIR / "state.json"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class WorkerInfo:
    """Config summary + identity. Populated by worker_loop at startup /
    after a cfg reload. Read by `dispatch` to route tasks."""

    worker_id: str = ""
    hostname: str = ""
    provider: str = ""
    agent_model_name: str = ""
    prompt_version: int | str | None = None
    cfg_loaded_at: str = ""


@dataclass
class CurrentTask:
    task_id: str
    task_name: str | None
    started_at: str
    unit: str
    pid: int | None = None


@dataclass
class QueuedTask:
    task_id: str
    task_name: str | None
    assigned_at: str


@dataclass
class CompletedTask:
    task_id: str
    task_name: str | None
    started_at: str
    finished_at: str
    status: str  # 'success' | 'failed' | 'timeout' | 'cancelled' | 'crashed'
    unit: str


@dataclass
class WorkerState:
    worker: WorkerInfo = field(default_factory=WorkerInfo)
    current: CurrentTask | None = None
    queue: list[QueuedTask] = field(default_factory=list)
    completed: list[CompletedTask] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker": asdict(self.worker),
            "current": asdict(self.current) if self.current else None,
            "queue": [asdict(q) for q in self.queue],
            "completed": [asdict(c) for c in self.completed],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkerState:
        worker_d = d.get("worker") or {}
        current_d = d.get("current")
        return cls(
            worker=WorkerInfo(
                **{
                    k: v
                    for k, v in worker_d.items()
                    if k in WorkerInfo.__dataclass_fields__
                }
            ),
            current=CurrentTask(**current_d) if current_d else None,
            queue=[QueuedTask(**q) for q in (d.get("queue") or [])],
            completed=[CompletedTask(**c) for c in (d.get("completed") or [])],
        )


# ---------------------------------------------------------------------------
# Locking + IO
# ---------------------------------------------------------------------------


def _now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _ensure_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def locked_state() -> Iterator[WorkerState]:
    """Open state.json under an exclusive flock. Yields a WorkerState the
    caller mutates in place; the mutated state is written back on context
    exit. Exceptions inside the block propagate and the file is left as it
    was (no partial write — we write once at the end).
    """
    _ensure_dir()
    # World-writable: the worker runs as root but SSH'd mutations come in as
    # ubuntu via gui-agents-queue. Single-user box — safe.
    fd = os.open(STATE_PATH, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        # Best-effort: only the owner (worker, running as root) can chmod.
        # Non-root callers inherit whatever mode the owner last set.
        try:
            os.fchmod(fd, 0o666)
        except PermissionError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 1 << 24).decode("utf-8").strip()
        data = json.loads(raw) if raw else {}
        state = WorkerState.from_dict(data)
        yield state
        blob = json.dumps(state.to_dict(), indent=2).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, blob)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def read_state() -> WorkerState:
    """Unlocked read — for display only. Returns an empty state if the file
    doesn't exist or is unparseable."""
    if not STATE_PATH.exists():
        return WorkerState()
    try:
        return WorkerState.from_dict(json.loads(STATE_PATH.read_text() or "{}"))
    except json.JSONDecodeError:
        return WorkerState()


# ---------------------------------------------------------------------------
# Mutation helpers — every caller uses these, so all writes go through flock
# ---------------------------------------------------------------------------


def set_worker_info(info: WorkerInfo) -> None:
    with locked_state() as s:
        info.cfg_loaded_at = info.cfg_loaded_at or _now()
        s.worker = info


def enqueue(task_id: str, task_name: str | None) -> bool:
    """Append to the queue. Idempotent: returns False if the task_id is
    already queued or currently running."""
    with locked_state() as s:
        if s.current and s.current.task_id == task_id:
            return False
        if any(q.task_id == task_id for q in s.queue):
            return False
        s.queue.append(
            QueuedTask(task_id=task_id, task_name=task_name, assigned_at=_now())
        )
        return True


def remove_from_queue(task_id: str) -> bool:
    with locked_state() as s:
        before = len(s.queue)
        s.queue = [q for q in s.queue if q.task_id != task_id]
        return len(s.queue) != before


def clear_queue() -> int:
    with locked_state() as s:
        n = len(s.queue)
        s.queue = []
        return n


def pop_head_as_current(unit: str) -> CurrentTask | None:
    """If idle and queue non-empty, move queue[0] into current. Atomic."""
    with locked_state() as s:
        if s.current is not None or not s.queue:
            return None
        head = s.queue.pop(0)
        s.current = CurrentTask(
            task_id=head.task_id,
            task_name=head.task_name,
            started_at=_now(),
            unit=unit,
        )
        return s.current


def set_current_pid(pid: int) -> None:
    with locked_state() as s:
        if s.current is not None:
            s.current.pid = pid


def finish_current(status: str) -> CompletedTask | None:
    """Move current → completed. Returns the CompletedTask, or None if
    current was already unset."""
    with locked_state() as s:
        cur = s.current
        if cur is None:
            return None
        done = CompletedTask(
            task_id=cur.task_id,
            task_name=cur.task_name,
            started_at=cur.started_at,
            finished_at=_now(),
            status=status,
            unit=cur.unit,
        )
        s.completed.append(done)
        s.current = None
        return done
