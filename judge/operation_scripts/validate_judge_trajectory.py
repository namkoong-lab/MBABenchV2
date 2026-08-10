"""Validate judge trajectory files: is this a complete, faithful record?

Judge-side analogue of coding-agents-master/tools/validate_trajectory.py.
Accepts a judge_results dir, a trajectory.jsonl, or a trajectory.jsonl.gz.

Six checks. Legitimate-but-unusual events become NOTES (pass); only genuine
corruption becomes a PROBLEM (fail):

  parse    every line is valid JSON; header + at least one call present
  blobs    every {"$blob": id} reference resolves to a blob record
  status   a call that errored and was never superseded by a later call in
           its category is a problem; superseded errors are retry notes
  chain    per category, request messages must not shrink between calls
           unless an evict_tool_results execution happened in between
           (eviction -> note); the first message must stay stable
  coverage every category in the header's check_order has an outcome;
           outcomes with pending checks are notes
  tokens   sum of per-call usage vs token_tracking.json within 0.8-1.25

Usage:
    python operation_scripts/validate_judge_trajectory.py <path> [<path> ...]
"""

import gzip
import json
import sys
from pathlib import Path

TOKEN_RATIO_MIN = 0.8
TOKEN_RATIO_MAX = 1.25


def open_traj(path):
    """Resolve a dir / .jsonl / .jsonl.gz to (lines, resolved_path)."""
    p = Path(path)
    if p.is_dir():
        for candidate in ("trajectory.jsonl.gz", "trajectory.jsonl"):
            if (p / candidate).exists():
                p = p / candidate
                break
        else:
            return None, p
    if p.suffix == ".gz":
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return f.read().splitlines(), p
    with open(p, encoding="utf-8") as f:
        return f.read().splitlines(), p


def _iter_blob_refs(obj):
    """Yield every blob id referenced anywhere in a record."""
    if isinstance(obj, dict):
        if set(obj.keys()) >= {"$blob"}:
            yield obj["$blob"]
        for v in obj.values():
            yield from _iter_blob_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_blob_refs(v)


def validate(path):
    problems, notes = [], []
    lines, resolved = open_traj(path)
    result = {"file": str(resolved), "problems": problems, "notes": notes}

    if lines is None:
        problems.append("no trajectory file found (grading aborted before any call?)")
        return result

    # -- parse --
    records = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            problems.append(f"line {i}: unparseable JSON ({e})")
    result["records"] = len(records)
    if not records:
        problems.append("no records")
        return result

    headers = [r for r in records if r.get("type") == "header"]
    calls = [r for r in records if r.get("type") == "call"]
    outcomes = [r for r in records if r.get("type") == "outcome"]
    blobs = {r["id"] for r in records if r.get("type") == "blob"}
    events = [r for r in records if r.get("type") == "event"]

    if not headers:
        problems.append("no header record")
    if not calls:
        problems.append("no call records")
    if not any(r.get("type") == "end" for r in records):
        notes.append("no end record — run may have crashed mid-grade (partial file)")

    # -- blobs --
    for r in records:
        if r.get("type") == "blob":
            continue
        for ref in _iter_blob_refs(r):
            if ref not in blobs:
                problems.append(f"step {r.get('step')}: dangling blob ref {ref}")

    # -- status + chain, per category --
    by_category = {}
    for c in calls:
        by_category.setdefault(c.get("category"), []).append(c)

    evict_steps = [
        e["step"]
        for e in events
        if e.get("kind") == "tool_exec" and e.get("tool") == "evict_tool_results"
    ]

    for category, cat_calls in by_category.items():
        errored = [c for c in cat_calls if c.get("error")]
        if errored:
            last = cat_calls[-1]
            if last.get("error"):
                problems.append(
                    f"{category}: final call errored ({last['error'][:120]})"
                )
            else:
                notes.append(
                    f"{category}: {len(errored)} transient API error(s), retried"
                )

        prev = None
        for c in cat_calls:
            msgs = (c.get("request") or {}).get("messages") or []
            if prev is not None:
                prev_msgs = (prev.get("request") or {}).get("messages") or []
                if len(msgs) < len(prev_msgs):
                    evicted_between = any(
                        prev["step"] < s < c["step"] for s in evict_steps
                    )
                    if evicted_between:
                        notes.append(
                            f"{category}: wire shrank {len(prev_msgs)}->{len(msgs)} "
                            f"after eviction (step {c['step']})"
                        )
                    else:
                        problems.append(
                            f"{category}: wire shrank {len(prev_msgs)}->{len(msgs)} "
                            f"with no eviction (step {c['step']})"
                        )
                if prev_msgs and msgs and json.dumps(msgs[0], sort_keys=True) != (
                    json.dumps(prev_msgs[0], sort_keys=True)
                ):
                    problems.append(
                        f"{category}: first wire message changed mid-run "
                        f"(step {c['step']})"
                    )
            prev = c

    # -- coverage --
    outcome_categories = {o.get("category") for o in outcomes}
    for header in headers[:1]:
        for category in header.get("check_order") or []:
            if category not in outcome_categories:
                problems.append(f"{category}: no outcome record")
    for o in outcomes:
        if o.get("pending"):
            notes.append(f"{o.get('category')}: finished with pending {o['pending']}")

    # -- tokens --
    traj_total = sum(
        (c.get("usage") or {}).get("total_tokens") or 0 for c in calls
    )
    telemetry = None
    for parent in (resolved.parent, resolved.parent.parent):
        tt = parent / "token_tracking.json"
        if tt.exists():
            with open(tt, encoding="utf-8") as f:
                telemetry = json.load(f)
            break
    if telemetry is None:
        notes.append("no token_tracking.json found — token cross-check skipped")
    elif traj_total and telemetry.get("total_tokens"):
        ratio = traj_total / telemetry["total_tokens"]
        if not (TOKEN_RATIO_MIN <= ratio <= TOKEN_RATIO_MAX):
            problems.append(
                f"token mismatch: trajectory {traj_total} vs telemetry "
                f"{telemetry['total_tokens']} (ratio {ratio:.2f})"
            )

    return result


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    any_failed = False
    for path in argv:
        r = validate(path)
        status = "FAIL" if r["problems"] else "PASS"
        if r["problems"]:
            any_failed = True
        print(f"{status}  {r['file']}  ({r.get('records', 0)} records)")
        for p in r["problems"]:
            print(f"  problem: {p}")
        for n in r["notes"]:
            print(f"  note:    {n}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
