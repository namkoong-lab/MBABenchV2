"""Tests for the LibreOffice memory guard (core/lo_guard.py; Patrick 2026-10-05: no size limit - one
LibreOffice at a time on the machine, started only when memory is free, a failed run retried with the
timeout doubled, then a loud failure to re-run later).

    cd judge && python -m detchecks.tests.test_lo_guard

No LibreOffice: run_once callables and fake memory readers.  The lock tests start two Python processes
that share a lock file in a temporary folder (never the machine-wide default).  Plain asserts; also
collectable by pytest.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from detchecks.core import lo_guard as G
from detchecks.errors import GradingError, LibreOfficeUnavailable

JUDGE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMPS: list = []


def tmpdir() -> str:
    d = tempfile.mkdtemp(prefix="detchecks_loguard_")
    _TMPS.append(d)
    return d


class Clock:
    """A fake clock + sleep: sleeping advances the clock."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def settings(lock, **kw):
    clock = kw.pop("clock", None) or Clock()
    return G.GuardSettings(lock_path=lock, sleep=clock.sleep, clock=clock.now, **kw), clock


# ============================================================================ defaults
def test_defaults_and_validation():
    s = G.GuardSettings()
    assert (s.min_free_pct, s.max_wait_s, s.retries) == (25.0, 3600.0, 3)
    old = os.environ.pop(G.LOCK_ENV, None)
    try:
        if os.name == "posix":
            assert G.default_lock_path() == "/tmp/mbabench_libreoffice.lock"     # one file for every process
        os.environ[G.LOCK_ENV] = "/somewhere/x.lock"
        assert G.default_lock_path() == "/somewhere/x.lock" and s.lock() == "/somewhere/x.lock"
    finally:
        os.environ.pop(G.LOCK_ENV, None)
        if old is not None:
            os.environ[G.LOCK_ENV] = old
    assert G.GuardSettings(lock_path="/a/b").lock() == "/a/b"
    assert G.GuardSettings(retries=-1).problems() and G.GuardSettings(min_free_pct=100).problems()
    assert G.GuardSettings(max_wait_s=-1).problems() and not G.GuardSettings(retries=0, min_free_pct=0).problems()
    try:
        G.run_guarded(lambda t: 1, timeout_s=1, settings=G.GuardSettings(retries=-1, lock_path=os.path.join(tmpdir(), "l")))
    except ValueError as e:
        assert "retries" in str(e)
    else:
        raise AssertionError("invalid settings accepted")
    pct = G.free_memory_pct()                         # this machine: a percentage, or None where unreadable
    assert pct is None or 0 <= pct <= 100, pct


# ============================================================================ memory wait
def test_memory_wait_low_then_ok():
    readings = iter([10.0, 18.0, 24.9, 31.0])
    logs = []
    s, clock = settings(os.path.join(tmpdir(), "l"), memory_reader=lambda: next(readings), poll_s=15, log_every_s=20)
    waited = G.wait_for_memory(s, what="recalculate x.xlsx", log=logs.append)
    assert waited == 45.0 and clock.sleeps == [15, 15, 15], (waited, clock.sleeps)
    assert any("waiting for memory" in m and "10% free < 25%" in m for m in logs), logs
    assert sum("waiting for memory" in m for m in logs) == 2, logs          # logged at most every 20 s
    assert "31% of memory free" in logs[-1] and "after waiting 45 s" in logs[-1], logs
    # enough memory at once: no wait, no log
    logs.clear()
    s, clock = settings(os.path.join(tmpdir(), "l"), memory_reader=lambda: 80.0)
    assert G.wait_for_memory(s, what="x", log=logs.append) == 0.0 and not logs and not clock.sleeps
    # memory that cannot be read: run without the wait (logged once)
    s, clock = settings(os.path.join(tmpdir(), "l"), memory_reader=lambda: None)
    G._UNKNOWN_MEMORY_LOGGED["done"] = False
    assert G.wait_for_memory(s, what="x", log=logs.append) == 0.0 and "cannot be read" in logs[0]
    # min_free_pct 0: never waits, never reads
    s, _ = settings(os.path.join(tmpdir(), "l"), min_free_pct=0, memory_reader=lambda: 1 / 0)
    assert G.wait_for_memory(s, what="x") == 0.0


def test_memory_wait_exceeded_fails_loudly():
    s, clock = settings(os.path.join(tmpdir(), "l"), memory_reader=lambda: 12.0, max_wait_s=120, poll_s=50)
    try:
        G.wait_for_memory(s, what="recalculate big.xlsx", log=lambda m: None)
    except LibreOfficeUnavailable as e:
        assert e.retry_later and isinstance(e, GradingError)
        assert "25% of its memory free within 2 min" in str(e) and "last reading 12%" in str(e), e
        assert "re-run this attempt when the machine has memory to spare" in str(e) and "not graded now" in str(e)
    else:
        raise AssertionError("no LibreOfficeUnavailable after the maximum wait")
    assert clock.t == 120 and clock.sleeps == [50, 50, 20], clock.sleeps
    # through run_guarded: LibreOffice is never started, no retry
    calls = []
    try:
        G.run_guarded(lambda t: calls.append(t), timeout_s=10, settings=s, what="x", log=lambda m: None)
    except LibreOfficeUnavailable:
        pass
    else:
        raise AssertionError("ran without memory")
    assert calls == []


# ============================================================================ retries
def test_retry_with_doubled_timeouts():
    s, _ = settings(os.path.join(tmpdir(), "l"), memory_reader=lambda: 90.0)
    seen = []

    def flaky(timeout):
        seen.append(timeout)
        if len(seen) < 3:
            raise GradingError(f"LibreOffice produced no copy (try {len(seen)})")
        return "copy.xlsx"

    logs = []
    assert G.run_guarded(flaky, timeout_s=600, settings=s, what="recalculate a.xlsx", log=logs.append) == "copy.xlsx"
    assert seen == [600, 1200, 2400], seen
    assert sum("retrying with a 1200 s timeout" in m for m in logs) == 1 and len(logs) == 2, logs
    # failing every time: retries + 1 tries, then LibreOfficeUnavailable listing each one
    seen.clear()

    def broken(timeout):
        seen.append(timeout)
        raise subprocess.TimeoutExpired("soffice", timeout)

    try:
        G.run_guarded(broken, timeout_s=10, settings=s, what="recalculate b.xlsx", log=lambda m: None)
    except LibreOfficeUnavailable as e:
        msg = str(e)
        assert seen == [10, 20, 40, 80] and e.retry_later, seen
        assert "all 4 tries failed" in msg and "try 1/4 (timeout 10 s" in msg and "try 4/4 (timeout 80 s" in msg, msg
        assert "TimeoutExpired" in msg and "re-run this attempt when the machine has memory to spare" in msg, msg
    else:
        raise AssertionError("no LibreOfficeUnavailable")
    # retries 0: one try
    s0, _ = settings(os.path.join(tmpdir(), "l"), memory_reader=lambda: 90.0, retries=0)
    seen.clear()
    try:
        G.run_guarded(broken, timeout_s=5, settings=s0, what="x", log=lambda m: None)
    except LibreOfficeUnavailable as e:
        assert seen == [5] and "all 1 tries failed" in str(e)
    # KeyboardInterrupt / SystemExit are never retried
    seen.clear()

    def interrupted(timeout):
        seen.append(timeout)
        raise KeyboardInterrupt

    try:
        G.run_guarded(interrupted, timeout_s=5, settings=s, what="x", log=lambda m: None)
    except KeyboardInterrupt:
        assert seen == [5]
    else:
        raise AssertionError("KeyboardInterrupt swallowed")


def test_memory_wait_before_every_try():
    readings = []

    def reader():
        readings.append(1)
        return 90.0

    s, _ = settings(os.path.join(tmpdir(), "l"), memory_reader=reader)
    n = {"tries": 0}

    def flaky(timeout):
        n["tries"] += 1
        if n["tries"] < 3:
            raise RuntimeError("crash")
        return 1

    G.run_guarded(flaky, timeout_s=1, settings=s, what="x", log=lambda m: None)
    assert len(readings) == 3, readings                 # one memory reading per try


# ============================================================================ the machine-wide lock
WORKER = r'''
import json, sys, time
sys.path.insert(0, {judge!r})
from detchecks.core import lo_guard as G
s = G.GuardSettings(lock_path={lock!r}, min_free_pct=0, retries=0)
def once(timeout):
    t0 = time.time()
    time.sleep({hold})
    return [t0, time.time()]
span = G.run_guarded(once, timeout_s=60, settings=s, what={what!r}, log=lambda m: print("LOG", m, flush=True))
print(json.dumps({{"who": {what!r}, "span": span}}), flush=True)
'''


def _spans(outs):
    rows = [json.loads(line) for o in outs for line in o.splitlines() if line.startswith("{")]
    return sorted((r["span"][0], r["span"][1], r["who"]) for r in rows)


def test_lock_serialises_two_processes():
    lock = os.path.join(tmpdir(), "mbabench_libreoffice.lock")
    procs = [subprocess.Popen([sys.executable, "-c", WORKER.format(judge=JUDGE, lock=lock, hold=1.5, what=f"job {k}")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for k in range(2)]
    outs = []
    for p in procs:
        so, se = p.communicate(timeout=60)
        assert p.returncode == 0, se[-800:]
        outs.append(so)
    spans = _spans(outs)
    assert len(spans) == 2, outs
    (a0, a1, _), (b0, b1, _) = spans
    assert a1 <= b0 + 0.05, f"the two LibreOffice runs overlapped: {spans}"
    waited = [o for o in outs if "waiting for the machine-wide LibreOffice lock" in o]
    assert waited and "held by pid" in waited[0], outs                     # the waiter says who holds it
    assert open(lock).read() == ""                                         # holder info cleared on release


def test_lock_threads_and_reentrancy():
    lock = os.path.join(tmpdir(), "l.lock")
    spans, logs = [], []

    def job(k):
        s = G.GuardSettings(lock_path=lock, min_free_pct=0, retries=0)

        def once(timeout):
            t0 = time.time()
            with G.machine_lock(lock, what="nested", log=logs.append):     # re-entrant: no self-deadlock
                time.sleep(0.4)
            spans.append((t0, time.time()))
        G.run_guarded(once, timeout_s=5, settings=s, what=f"thread {k}", log=logs.append)

    ts = [threading.Thread(target=job, args=(k,)) for k in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    spans.sort()
    assert len(spans) == 3 and all(spans[i][1] <= spans[i + 1][0] + 0.05 for i in range(2)), spans
    assert any("another grading thread of this process" in m for m in logs), logs
    # the lock is released after an exception inside it
    try:
        with G.machine_lock(lock, log=lambda m: None):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with G.machine_lock(lock, log=lambda m: None) as held:
        assert held.waited_s < 1.0


def test_lock_wait_is_capped():
    """Patrick 2026-10-05 (every attempt graded): the wait for the machine-wide lock is capped
    (max_lock_wait_s, default 3 hours); then LibreOfficeUnavailable (retry_later), LibreOffice never
    started.  Fakes: the lock file held through another descriptor, a thread holding the lock, a fake clock."""
    import fcntl
    assert G.GuardSettings().max_lock_wait_s == 3 * 3600.0
    assert G.GuardSettings(max_lock_wait_s=-1).problems()
    lock = os.path.join(tmpdir(), "l.lock")
    calls = []
    # (1) another process holds the lock file (a second open file description of it): the default 3-hour cap,
    #     reached on the fake clock
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o666)
    os.write(fd, b"pid 4242 on elsewhere to recalculate stuck.xlsx\n")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        s, clock = settings(lock, min_free_pct=0)
        logs = []
        try:
            G.run_guarded(lambda t: calls.append(t), timeout_s=60, settings=s, what="recalculate a.xlsx",
                          log=logs.append)
        except LibreOfficeUnavailable as e:
            assert e.retry_later and isinstance(e, GradingError)
            msg = str(e)
            assert "lock" in msg and "180 min" in msg and "held by pid 4242 on elsewhere" in msg, msg
            assert "re-run this attempt later" in msg and "not graded now" in msg, msg
        else:
            raise AssertionError("no LibreOfficeUnavailable after the maximum lock wait")
        assert calls == [] and 3 * 3600.0 <= clock.t < 3 * 3600.0 + 1, clock.t
        assert any("held by pid 4242" in m for m in logs) and len(logs) < 200, len(logs)     # logged once a minute
        # a shorter configured cap, with real time
        s2 = G.GuardSettings(lock_path=lock, min_free_pct=0, max_lock_wait_s=0.6, retries=0)
        t0 = time.monotonic()
        try:
            G.run_guarded(lambda t: calls.append(t), timeout_s=60, settings=s2, what="x", log=lambda m: None)
        except LibreOfficeUnavailable:
            assert 0.5 <= time.monotonic() - t0 < 5, time.monotonic() - t0
        else:
            raise AssertionError("no LibreOfficeUnavailable after a 0.6 s lock wait")
        assert calls == []
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # (2) another grading thread of this process holds the lock: the same cap on the process-wide lock
    held, release = threading.Event(), threading.Event()

    def holder():
        with G.machine_lock(lock, what="hold", log=lambda m: None):
            held.set()
            release.wait(30)

    th = threading.Thread(target=holder)
    th.start()
    try:
        assert held.wait(10)
        s3, clock3 = settings(lock, min_free_pct=0, max_lock_wait_s=600)
        try:
            G.run_guarded(lambda t: calls.append(t), timeout_s=60, settings=s3, what="y", log=lambda m: None)
        except LibreOfficeUnavailable as e:
            assert e.retry_later and "another grading thread of this process" in str(e) and "10 min" in str(e), e
        else:
            raise AssertionError("no LibreOfficeUnavailable while another thread holds the lock")
        assert calls == [] and 600 <= clock3.t < 601, clock3.t
    finally:
        release.set()
        th.join(timeout=30)
    # (3) once the lock is free a capped wait takes it at once, and the run goes ahead
    s4, _ = settings(lock, min_free_pct=0, max_lock_wait_s=1)
    assert G.run_guarded(lambda t: "copy.xlsx", timeout_s=60, settings=s4, what="z", log=lambda m: None) == "copy.xlsx"


def test_lock_file_is_not_inherited():
    """LibreOffice (a child) must never keep the lock: the descriptor is close-on-exec."""
    lock = os.path.join(tmpdir(), "l.lock")
    with G.machine_lock(lock, log=lambda m: None) as held:
        fd = held._fd
        assert not os.get_inheritable(fd)
        child = subprocess.run([sys.executable, "-c", f"import os; os.fstat({fd})"], capture_output=True, close_fds=False)
        assert child.returncode != 0                                       # the child has no such descriptor


TESTS = [test_defaults_and_validation, test_memory_wait_low_then_ok, test_memory_wait_exceeded_fails_loudly,
         test_retry_with_doubled_timeouts, test_memory_wait_before_every_try, test_lock_serialises_two_processes,
         test_lock_threads_and_reentrancy, test_lock_wait_is_capped, test_lock_file_is_not_inherited]


def _cleanup():
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)


try:
    import pytest

    @pytest.fixture(autouse=True, scope="module")
    def _cleanup_module():
        yield
        _cleanup()
except ImportError:  # plain script run without pytest
    pass


def main() -> int:
    failed = 0
    try:
        for t in TESTS:
            try:
                t()
                print(f"PASS {t.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}")
                traceback.print_exc()
    finally:
        _cleanup()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
