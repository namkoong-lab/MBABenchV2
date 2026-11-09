#!/usr/bin/env python
"""Provider-reported token usage per agent cohort, for the in-house and coding harnesses.

Answers the reviewer's "per-agent breakdown of tokens used per run" with the numbers the
model APIs themselves returned — no price tables, no cache-rate assumptions. Cost is left out
on purpose: only Claude Code reports it; the other lanes would need tokens x list price.

Cohorts: the leaderboard's registered agents (operation/v2/style_guide.yaml `agents`) whose
attempts ran under the `api` (in-house) or `coding_cli` (Claude Code / Codex) harness. GUI and
Excel add-in cohorts are subscription products with no token report and are skipped.

Attempts: by default the same attempt per (cohort, task) that the leaderboard scores — the
newest clean grading by the current judge (plot_leaderboard.MIN_JUDGE_VERSION), attempt and task
not deprecated. --all-attempts widens to every clean, non-failed attempt of the cohort.

Where the tokens come from, per harness (all read from the attempt's files on S3):
  coding_cli  <stamp>_telemetry.json, written by coding_agent/telemetry.py from the CLI's own
              event stream: Claude Code's `result` event usage (Anthropic counts: input_tokens
              EXCLUDES cache_read / cache_creation), Codex's `turn.completed` usage (OpenAI
              counts: input_tokens INCLUDES cached_input_tokens).
  api         agent_logs/openai_requests.csv, one row per model call with the response's
              usage.prompt_tokens / completion_tokens (no prompt caching was used, so these are
              full counts). Anthropic-direct runs write a shorter 6-column row with the same
              numbers (usage.input_tokens / output_tokens); both layouts are read.

The one table: rows are Agent and Harness worded as the mbabench.org leaderboard ("Astra" /
"Codex", "Fable 5.1" / "In-house harness"), columns the
25th percentile, median and 75th percentile of total tokens per task attempt (input + output),
ranked by median, and the cohort's leaderboard pass rate (pass_rule.py on the same gradings). Input is everything the model read across the run (every call re-reads the
whole conversation, so this grows with the number of steps; cache hits are counted, since the
provider counts them), normalised so the harnesses compare like for like: Claude Code
input + cache_read + cache_creation, Codex input_tokens, in-house sum of prompt_tokens. Output is
every generated token, thinking / reasoning included (all providers count it there).

Quartiles rather than mean and standard deviation: the distributions are right-skewed (a few
marathon runs), so the mean overstates the typical run and the SD mostly measures the tail.

Writes under operation/results/v2/token_usage/ (gitignored):
  tokens_per_task.md / .csv   the table
  attempts.csv                one row per attempt with the normalised and raw provider fields, for audit
Downloads are cached under operation/results/v2/token_usage/cache/ keyed by S3 path.

Run from the repo root with the judge environment (psycopg2, boto3, yaml):
    uv run --project judge python operation/v2/paper_scripts/token_usage.py
    uv run --project judge python operation/v2/paper_scripts/token_usage.py --all-attempts
    uv run --project judge python operation/v2/paper_scripts/token_usage.py --database-url postgresql://...
"""

import argparse
import csv
import io
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import boto3
import psycopg2
import psycopg2.extras
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pass_rule import load_tiers, task_verdict  # noqa: E402  the paper's pass rule

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
OUT_DIR = REPO_ROOT / "operation" / "results" / "v2" / "token_usage"
CACHE_DIR = OUT_DIR / "cache"
STYLE_GUIDE = REPO_ROOT / "operation" / "v2" / "style_guide.yaml"
CONFIG = REPO_ROOT / "config" / "config.yaml"
BUCKET = "mbabench"

# Same floor as plot_leaderboard.MIN_JUDGE_VERSION (imported lazily: that module needs matplotlib).
MIN_JUDGE_VERSION = 12
HARNESS = {"api": "In-house", "coding_cli": "Code"}
# As plot_leaderboard.BACKFILL_ZERO: cohorts whose ungraded live tasks count as failed, so their pass
# rate sits on the full task set (plot_leaderboard itself is not imported: it needs matplotlib).
BACKFILL_ZERO = {"Fable 5.1", "Qwen 3.8", "Qwen 3.8-Code"}
# mbabench.org leaderboard wording for the Harness column, by agent_model_name prefix.
HARNESS_LABEL = {"claudecode_": "Claude Code", "codex_": "Codex", "openpyxl_": "In-house harness"}
# Pooled cohorts the website names by one harness. Opus 5-Code pools Claude Code (36 tasks) and
# Codex via Forge (the rest); mbabench.org lists it as "Claude Code".
HARNESS_OVERRIDE = {"Opus 5-Code": "Claude Code"}


def harness_label(agent_model_name: str) -> str:
    for prefix, label in HARNESS_LABEL.items():
        if agent_model_name.startswith(prefix):
            return label
    return agent_model_name.split("/")[0]
TOKEN_HARNESSES = tuple(HARNESS)
DOWNLOAD_THREADS = 16
csv.field_size_limit(sys.maxsize)  # openai_requests.csv rows carry whole message bodies

# The leaderboard's attempt per (agent_model_name, task): newest clean grading by the current judge.
SQL_LEADERBOARD = """
    select distinct on (ta.agent_model_name, ta.task_id)
           ta.id as attempt_id, ta.task_id, ta.agent_model_name, ta.agent_model_type,
           ta.attempt_files, ta.time_taken_min, g.created_at as graded_at,
           g.scored_results->'check_scores' as check_scores
    from gradings g
    join task_attempts ta on ta.id = g.attempt_id
    join tasks t on t.id = ta.task_id
    where g.judge_version >= %(judge)s
      and g.failed is not true and g.deprecated is not true
      and ta.deprecated is not true and t.deprecated is not true
      and g.scored_results->>'total_score' is not null
      and ta.agent_model_name = any(%(names)s)
    order by ta.agent_model_name, ta.task_id, g.created_at desc
"""

# Every clean, finished attempt of the cohort (--all-attempts).
SQL_ALL = """
    select ta.id as attempt_id, ta.task_id, ta.agent_model_name, ta.agent_model_type,
           ta.attempt_files, ta.time_taken_min, ta.created_at as graded_at,
           null::json as check_scores
    from task_attempts ta
    join tasks t on t.id = ta.task_id
    where ta.deprecated is not true and t.deprecated is not true
      and ta.agent_failed is not true
      and ta.agent_model_name = any(%(names)s)
    order by ta.agent_model_name, ta.task_id, ta.id
"""


@dataclass
class Usage:
    """One attempt's provider-reported tokens, normalised (see module docstring)."""

    cohort: str
    vendor: str
    harness: str
    agent_model_name: str
    attempt_id: int
    task_id: int
    input_total: int
    input_cached: int
    output: int
    reasoning: int | None
    calls: int | None
    minutes: float | None
    source: str  # the S3 file the numbers came from
    raw: str  # the provider fields as JSON, for audit
    note: str = ""
    passed: bool | None = None  # the paper's pass rule on this attempt's grading; None = undecidable / ungraded


# --------------------------------------------------------------------------- inputs


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text()) if CONFIG.exists() else {}


def database_url(args) -> str:
    if args.database_url:
        return args.database_url
    url = load_config().get("database", {}).get("v2_url")
    if not url:
        sys.exit("config/config.yaml has no database.v2_url — pass --database-url.")
    return url


COHORT_FIRST_IDENTITY: dict[str, str] = {}  # display name -> first agent_model_name listed for it


def cohorts() -> dict[str, tuple[str, str]]:
    """agent_model_name -> (display name, vendor) for every registered cohort."""
    agents = yaml.safe_load(STYLE_GUIDE.read_text())["agents"]
    out: dict[str, tuple[str, str]] = {}
    for name, v in agents.items():
        COHORT_FIRST_IDENTITY[name] = v["db"][0]
        for db in v["db"]:
            out[db] = (name, v["vendor"])
    return out


def fetch(conn, registry: dict[str, tuple[str, str]], all_attempts: bool) -> tuple[list[dict], int]:
    """(attempt rows, number of live tasks). Pooled cohorts keep the newest grading per task, as the leaderboard."""
    sql = SQL_ALL if all_attempts else SQL_LEADERBOARD
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"judge": MIN_JUDGE_VERSION, "names": list(registry)})
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute("select count(*) as n from tasks where deprecated is not true")
        live = cur.fetchone()["n"]
    rows = [r for r in rows if r["agent_model_type"] in TOKEN_HARNESSES]
    if not all_attempts:
        newest: dict[tuple[str, int], dict] = {}
        for r in rows:
            key = (registry[r["agent_model_name"]][0], r["task_id"])
            if key not in newest or r["graded_at"] > newest[key]["graded_at"]:
                newest[key] = r
        rows = list(newest.values())
    return rows, live


# --------------------------------------------------------------------------- S3


_s3 = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def cached_get(key: str) -> str | None:
    """The object's text, from the local cache or S3; None when the key does not exist."""
    key = key.removeprefix(f"s3://{BUCKET}/")
    path = CACHE_DIR / key
    if path.exists():
        return path.read_text(errors="replace")
    try:
        body = s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except s3().exceptions.NoSuchKey:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body.decode("utf-8", errors="replace")


def file_keys(attempt_files) -> list[str]:
    """Every s3:// path in the attempt_files JSON, whatever its shape."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, str):
            if node.startswith("s3://"):
                found.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(attempt_files)
    return found


def pick(keys: list[str], suffix: str) -> str | None:
    hits = [k for k in keys if k.endswith(suffix)]
    return hits[0] if hits else None


# --------------------------------------------------------------------------- parsers


def usage_coding(row: dict, meta: tuple[str, str]) -> Usage:
    name, vendor = meta
    base = dict(
        cohort=name,
        vendor=vendor,
        harness=HARNESS[row["agent_model_type"]],
        agent_model_name=row["agent_model_name"],
        attempt_id=row["attempt_id"],
        task_id=row["task_id"],
        minutes=row["time_taken_min"],
    )
    key = pick(file_keys(row["attempt_files"]), "_telemetry.json")
    if not key:
        return Usage(**base, input_total=0, input_cached=0, output=0, reasoning=None, calls=None,
                     source="", raw="{}", note="no telemetry.json in attempt_files")
    text = cached_get(key)
    if text is None:
        return Usage(**base, input_total=0, input_cached=0, output=0, reasoning=None, calls=None,
                     source=key, raw="{}", note="telemetry.json missing on S3")
    tel = json.loads(text)
    tot = tel.get("totals") or {}
    if tel.get("parse_error") or not tot:
        return Usage(**base, input_total=0, input_cached=0, output=0, reasoning=None, calls=None,
                     source=key, raw=json.dumps(tot), note=f"telemetry parse error: {tel.get('parse_error')}")
    if tel.get("parser") == "claude":
        # Anthropic counts: input_tokens excludes the cache fields.
        cache_read = tot.get("cache_read_input_tokens", 0)
        cache_create = tot.get("cache_creation_input_tokens", 0)
        return Usage(
            **base,
            input_total=tot.get("input_tokens", 0) + cache_read + cache_create,
            input_cached=cache_read,
            output=tot.get("output_tokens", 0),
            reasoning=(tot.get("output_tokens_details") or {}).get("thinking_tokens"),
            calls=tel.get("num_turns"),
            source=key,
            raw=json.dumps({k: tot.get(k) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                "output_tokens", "output_tokens_details")}),
        )
    # Codex: OpenAI counts, input_tokens already includes cached_input_tokens. One prompt per run.
    return Usage(
        **base,
        input_total=tot.get("input_tokens", 0),
        input_cached=tot.get("cache_read_tokens", 0),
        output=tot.get("output_tokens", 0),
        reasoning=tot.get("reasoning_output_tokens"),
        calls=tel.get("num_turns"),
        source=key,
        raw=json.dumps(tot),
    )


def usage_inhouse(row: dict, meta: tuple[str, str]) -> Usage:
    name, vendor = meta
    base = dict(
        cohort=name,
        vendor=vendor,
        harness=HARNESS[row["agent_model_type"]],
        agent_model_name=row["agent_model_name"],
        attempt_id=row["attempt_id"],
        task_id=row["task_id"],
        minutes=row["time_taken_min"],
    )
    key = pick(file_keys(row["attempt_files"]), "openai_requests.csv")
    if not key:
        return Usage(**base, input_total=0, input_cached=0, output=0, reasoning=None, calls=None,
                     source="", raw="{}", note="no openai_requests.csv in attempt_files")
    text = cached_get(key)
    if text is None:
        return Usage(**base, input_total=0, input_cached=0, output=0, reasoning=None, calls=None,
                     source=key, raw="{}", note="openai_requests.csv missing on S3")
    # Two row layouts share one header: OpenAI-compatible calls write all 14 columns
    # (prompt_tokens at 10, completion_tokens at 11); Anthropic-direct calls write 6
    # (timestamp, model, prompt_tokens, completion_tokens, total_tokens, cost_usd).
    prompt = completion = calls = 0
    for i, r in enumerate(csv.reader(io.StringIO(text))):
        if i == 0 and r and r[0] == "timestamp":
            continue
        if len(r) == 14:
            p, c = r[10], r[11]
        elif len(r) == 6:
            p, c = r[2], r[3]
        else:
            continue
        try:
            prompt += int(float(p or 0))
            completion += int(float(c or 0))
        except ValueError:
            continue
        calls += 1
    return Usage(
        **base,
        input_total=prompt,
        input_cached=0,
        output=completion,
        reasoning=None,
        calls=calls,
        source=key,
        raw=json.dumps({"prompt_tokens": prompt, "completion_tokens": completion, "rows": calls}),
        note="" if calls else "empty request log",
    )


def usage_for(row: dict, meta: tuple[str, str]) -> Usage:
    fn = usage_coding if row["agent_model_type"] == "coding_cli" else usage_inhouse
    return fn(row, meta)


# --------------------------------------------------------------------------- summary


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """(25th percentile, median, 75th percentile), inclusive method as a spreadsheet's QUARTILE.INC."""
    q = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else [values[0]] * 3
    return q[0], q[1], q[2]


def summarise(usages: list[Usage], live_tasks: int) -> list[dict]:
    """One row per cohort: n, the quartiles of total tokens (input + output) per task, and the pass rate, most tokens first.

    Pass rate as the leaderboard computes it: passes over the tasks with a decidable verdict, plus
    (BACKFILL_ZERO cohorts) every live task without a grading counted as failed.
    """
    by: dict[str, list[Usage]] = {}
    for u in usages:
        if not u.note:  # attempts without a token record are counted but not averaged
            by.setdefault(u.cohort, []).append(u)
    missing: dict[str, int] = {}
    for u in usages:
        if u.note:
            missing[u.cohort] = missing.get(u.cohort, 0) + 1
    rows = []
    for cohort, us in by.items():
        q1, med, q3 = quartiles([u.input_total + u.output for u in us])
        # A pooled cohort is named by one harness, as on mbabench.org (HARNESS_OVERRIDE).
        labels = [HARNESS_OVERRIDE.get(cohort) or harness_label(COHORT_FIRST_IDENTITY[cohort])]
        graded = [u for u in usages if u.cohort == cohort]  # token record or not
        decided = [u.passed for u in graded if u.passed is not None]
        denom = len(decided) + (max(live_tasks - len(graded), 0) if cohort in BACKFILL_ZERO else 0)
        pass_rate = (sum(decided) / denom) if denom else None
        rows.append({
            "agent": cohort.removesuffix("-Code"),
            "harness": labels[0],
            "n": len(us),
            "n_missing": missing.get(cohort, 0),
            "p25": q1,
            "median": med,
            "p75": q3,
            "pass_rate": pass_rate,
        })
    rows.sort(key=lambda r: -r["median"])
    return rows


def fmt_m(x: float) -> str:
    return f"{x / 1_000_000:.2f}M"


def markdown(rows: list[dict]) -> str:
    head = ["Agent", "Harness", "n", "25th pct", "Median", "75th pct", "Pass rate"]
    lines = ["| " + " | ".join(head) + " |", "|---|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        n = f"{r['n']}" + (f" (+{r['n_missing']} no record)" if r["n_missing"] else "")
        lines.append("| " + " | ".join([
            r["agent"], r["harness"], n, fmt_m(r["p25"]), fmt_m(r["median"]), fmt_m(r["p75"]),
            "–" if r["pass_rate"] is None else f"{100 * r['pass_rate']:.1f}%",
        ]) + " |")
    lines += [
        "",
        "Total tokens per task attempt (input + output, as reported by the model API), ranked by median. "
        "Input is everything the model read across the run, cache hits included; output includes "
        "thinking / reasoning tokens. Pass rate is the leaderboard's (pass_rule.py) over the same attempts.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", help="full connection string; bypasses config/config.yaml")
    ap.add_argument("--all-attempts", action="store_true",
                    help="every clean, non-failed attempt instead of the leaderboard's one per task")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    registry = cohorts()
    conn = psycopg2.connect(database_url(args))
    try:
        rows, live_tasks = fetch(conn, registry, args.all_attempts)
    finally:
        conn.close()
    if not rows:
        sys.exit("no attempts found")
    print(f"{len(rows)} attempts across {len({r['agent_model_name'] for r in rows})} agent identities; "
          f"reading token records from S3 ...", file=sys.stderr)

    with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
        usages = list(pool.map(lambda r: usage_for(r, registry[r["agent_model_name"]]), rows))
    tiers = load_tiers()
    for u, r in zip(usages, rows):
        verdict = task_verdict(r["check_scores"], tiers)
        u.passed = None if verdict is None else verdict.passed

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "attempts.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(usages[0]).keys()))
        w.writeheader()
        for u in sorted(usages, key=lambda u: (u.harness, u.cohort, u.task_id)):
            w.writerow(asdict(u))

    summary = summarise(usages, live_tasks)
    with (args.out_dir / "tokens_per_task.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)
    md = markdown(summary)
    (args.out_dir / "tokens_per_task.md").write_text(md + "\n")
    print(md)

    notes = [u for u in usages if u.note]
    if notes:
        print(f"\n{len(notes)} attempts without a usable token record:", file=sys.stderr)
        for u in notes[:20]:
            print(f"  {u.cohort} attempt {u.attempt_id} task {u.task_id}: {u.note}", file=sys.stderr)
        if len(notes) > 20:
            print(f"  ... and {len(notes) - 20} more (see attempts.csv `note`)", file=sys.stderr)
    print(f"\nwrote {args.out_dir / 'tokens_per_task.md'}, tokens_per_task.csv, attempts.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
