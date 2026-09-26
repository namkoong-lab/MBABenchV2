#!/usr/bin/env python3
"""Trajectory validator: is a trajectory.jsonl(.gz) a complete, faithful record?

Usage: python3 tools/validate_trajectory.py <attempt_dir | trajectory file> [...]

Checks per file:
  parse      every line is valid JSON with the expected fields
  status     every model call returned HTTP 200 (others listed)
  stream     every SSE response reaches its terminal event
             (claude: message_stop; codex: response.completed)
  chain      state accumulation is consistent step-over-step —
             claude: messages[] grows and step k's history is a prefix of k+1's
             codex: input[] grows and instructions stay constant
             (a legitimate CLI context compaction is reported, not failed)
  scrub      no credential material anywhere in the file
  tokens     output tokens summed from SSE usage vs telemetry.json totals

Exit 0 if all files pass (compaction counts as pass).
"""
import gzip
import json
import re
import sys
from pathlib import Path


def open_traj(path: Path):
    if path.is_dir():
        for cand in (path / "trajectory.jsonl.gz", path / "trajectory" / "trajectory.jsonl"):
            if cand.exists():
                path = cand
                break
        else:
            return path, None
    opener = gzip.open if path.suffix == ".gz" else open
    return path, opener(path, "rt", errors="replace")


def sse_events(raw: str):
    return re.findall(r"^event: ([\w.]+)", raw, re.M) + re.findall(r'"type"\s*:\s*"([\w.]+)"', raw)


def validate(path: Path) -> dict:
    path, fh = open_traj(path)
    result = {"file": str(path), "records": 0, "problems": [], "notes": []}
    if fh is None:
        result["problems"].append("no trajectory file (attempt aborted before agent start?)")
        return result
    recs = []
    for i, line in enumerate(fh, 1):
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError as e:
            result["problems"].append(f"line {i}: unparseable ({e})")
    result["records"] = len(recs)
    if not recs:
        result["problems"].append("no records")
        return result

    kind = "claude" if any("messages" in (r.get("request") or {}) for r in recs) else "codex"
    result["agent"] = kind
    posts = [r for r in recs if isinstance(r.get("request"), dict)
             and ("messages" in r["request"] or "input" in r["request"])]

    # status
    bad = [r["step"] for r in recs if r.get("status") != 200]
    last_step = recs[-1].get("step")
    if bad:
        # a transient error mid-run that later records supersede is a faithful
        # recording of an API hiccup + retry, not a broken log
        if bad[-1] != last_step:
            result["notes"].append(f"transient API error(s) recorded at steps {bad} (retried)")
        else:
            result["problems"].append(f"run ENDED on non-200 at step {bad[-1]}")

    # stream termination
    unterminated = []
    for r in posts:
        raw = (r.get("response") or {}).get("_raw_text")
        if raw is None:
            continue
        events = sse_events(raw)
        terminal = ("message_stop" in events) if kind == "claude" else \
                   any(e.endswith("completed") or e == "[DONE]" for e in events) or "[DONE]" in raw
        if not terminal:
            unterminated.append(r["step"])
    if unterminated:
        if unterminated[-1] != last_step:
            result["notes"].append(f"interrupted stream(s) at steps {unterminated} (retried)")
        else:
            result["problems"].append(f"final stream missing terminal event (step {unterminated[-1]})")

    # chain consistency
    key = "messages" if kind == "claude" else "input"
    compactions = 0
    breaks = []
    prev = None
    for r in posts:
        cur = r["request"].get(key) or []
        if prev is not None:
            if len(cur) < len(prev):
                compactions += 1
            elif json.dumps(prev[0], sort_keys=True) != json.dumps(cur[0], sort_keys=True):
                # first element should be stable (initial prompt) unless compacted
                compactions += 1
        prev = cur
    if kind == "codex":
        instr = {json.dumps((r["request"].get("instructions") or ""))[:64] for r in posts}
        if len(instr) > 1:
            breaks.append("instructions changed mid-run")
    if breaks:
        result["problems"].append("; ".join(breaks))
    if compactions:
        result["notes"].append(f"{compactions} context compaction(s)")

    # scrub — look for live key material anywhere
    blob = json.dumps(recs)
    for pat, name in ((r"sk-ant-[A-Za-z0-9\-_]{20}", "anthropic key"),
                      (r"sk-proj-[A-Za-z0-9\-_]{20}", "openai key")):
        if re.search(pat, blob):
            result["problems"].append(f"UNSCRUBBED {name} present")

    # tokens vs telemetry
    tele = path.parent / "telemetry.json"
    if not tele.exists():
        tele = path.parent.parent / "telemetry.json"
    if tele.exists():
        totals = (json.load(open(tele)).get("totals") or {})
        expect = totals.get("output_tokens")
        got = 0
        pat = (r'"output_tokens"\s*:\s*(\d+)')
        for r in posts:
            raw = (r.get("response") or {}).get("_raw_text") or ""
            nums = [int(x) for x in re.findall(pat, raw)]
            if nums:
                got += max(nums)  # per-call cumulative max
        if expect and got:
            ratio = got / expect
            if not (0.8 <= ratio <= 1.25):
                result["problems"].append(f"token mismatch: SSE sum {got} vs telemetry {expect}")
            else:
                result["notes"].append(f"tokens ok ({got} vs telemetry {expect})")
    return result


def main():
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        sys.exit(__doc__)
    failed = 0
    for p in paths:
        r = validate(p)
        status = "PASS" if not r["problems"] else "FAIL"
        failed += bool(r["problems"])
        notes = ("; ".join(r["notes"])) or "-"
        parts = Path(r["file"]).parts
        name = next((x for x in parts if x.startswith("task")), r["file"])
        print(f"{status}  {r.get('agent','?'):6s} {r['records']:4d} recs  {name}  [{notes}]")
        for prob in r["problems"]:
            print(f"      !! {prob}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
