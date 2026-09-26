#!/usr/bin/env python3
"""Export the offline benchmark bundle under data/ (see data/README.md).

READ-ONLY against the database and the object store: plain autocommit SELECTs
on the pooler host (never SET / BEGIN), GET and LIST on the bucket. Every write
goes under data/. Idempotent: a file already present with the expected size is
hashed, not downloaded again.

Credentials come from config/config.yaml only (database.v2_url, aws.*); the
script hard-codes none and never prints one.

What it writes (all tracked in git unless marked sidecar):

  data/tasks/…                 the tasks table + every starting / golden file, byte for byte
  data/results/task_attempts.jsonl, gradings.jsonl.gz, judge_annotations.jsonl
                               every row the leaderboard, the ablations and the paper read
  data/results/annotations/…   the human annotation bundles (annotator names replaced by ids)
  data/results/experiments/…   the consolidated stage 2/3/4/5 gradings and the attempt-id
                               lists the judge experiments are re-run from
  data/results/ATTEMPT_FILES_MANIFEST.json
                               size / etag / sha256 (when fetched) of every attempt artifact
  data/results/attempts/<id>/… sidecar, only with --attempt-files workbooks|all
  data/judge_reliability/*.jsonl
                               the judge-reliability toy tables
  data/judge_reliability/toy_tasks/…
                               sidecar, only with --toy-files
  data/MANIFEST.json           path, original object key, size, sha256 of every tracked file

    uv run python scripts/export_benchmark_data.py
    uv run python scripts/export_benchmark_data.py --only tasks
    uv run python scripts/export_benchmark_data.py --attempt-files workbooks
    uv run python scripts/export_benchmark_data.py --attempt-files all --toy-files
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from config import Config  # noqa: E402

EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}
BUCKET_PLACEHOLDER = "<bucket>"
STAGES_NOTE = (
    "Consolidated gradings of the additional experiments. stage 2 = 90 attempts (30 tasks x 3 agents) "
    "graded three times by the leaderboard judge: sol_production is the first live grading, sol_repeat the "
    "other two in grading-id order. stage 3 = the same three agents graded by a second, more expensive judge "
    "(fable_judge) next to their production grading (sol_production). stage 4 = the coding Fable 5.1 low / "
    "high / max effort arms, one production grading each. stage 5 = the coding prompt ablation: template v13 "
    "on the Claude Code route (the leaderboard cohort) against v14 and v15 on the Codex route. "
    "total_score is scored_results.total_score; per-check verdicts are in gradings.jsonl."
)


# ----------------------------------------------------------------------------- helpers
def jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    raise TypeError(f"not JSON serializable: {type(v).__name__}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def write_jsonl(path: Path, rows, scrub) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(scrub(json.dumps(r, default=jsonable, ensure_ascii=False)) + "\n")


def write_jsonl_gz(path: Path, rows, scrub) -> None:
    """write_jsonl, gzip-compressed with no name / mtime in the header (deterministic bytes, like gzip -n)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as f:
        for r in rows:
            f.write((scrub(json.dumps(r, default=jsonable, ensure_ascii=False)) + "\n").encode("utf-8"))


def write_json(path: Path, obj, scrub) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(json.dumps(obj, indent=2, default=jsonable, ensure_ascii=False)) + "\n", encoding="utf-8")


class Store:
    """Thin read-only wrapper over the bucket: list, fetch (with hashing), key rewriting."""

    def __init__(self, cfg: Config):
        self.bucket = cfg.get("aws.s3_bucket")
        if not self.bucket or self.bucket == BUCKET_PLACEHOLDER:
            sys.exit("aws.s3_bucket is not configured in config/config.yaml")
        kw = {}
        if cfg.get("aws.access_key_id"):
            kw["aws_access_key_id"] = cfg.get("aws.access_key_id")
            kw["aws_secret_access_key"] = cfg.get("aws.secret_access_key")
        self.s3 = boto3.client("s3", **kw)
        self._uri_prefix = f"s3://{self.bucket}/"
        self._bare = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(self.bucket) + r"(?![A-Za-z0-9_-])")
        self.downloaded = 0
        self.bytes = 0

    def key(self, uri: str) -> str:
        if uri.startswith(self._uri_prefix):
            return uri[len(self._uri_prefix):]
        if uri.startswith("s3://"):
            raise ValueError(f"object in another bucket: {uri}")
        return uri.lstrip("/")

    def scrub(self, text: str) -> str:
        """Replace the bucket name wherever it appears in exported text: as an object URI and as a
        standalone token. Tokens that merely start with it (a Docker image tag such as
        <name>-coding-agent:v3, a schema called <name>v2) are not the bucket and stay as they are."""
        text = text.replace(self._uri_prefix, f"s3://{BUCKET_PLACEHOLDER}/")
        return self._bare.sub(BUCKET_PLACEHOLDER, text)


    def listing(self, prefix: str) -> dict[str, dict]:
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out[o["Key"]] = {"size": o["Size"], "etag": o["ETag"].strip('"')}
        return out

    def fetch(self, key: str, dest: Path, expected_size: int | None = None) -> tuple[int, str]:
        """Download key to dest unless a file of the expected size is already there. Returns (size, sha256)."""
        if expected_size is None:
            expected_size = self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        if not (dest.exists() and dest.stat().st_size == expected_size):
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            self.s3.download_file(self.bucket, key, str(tmp))
            os.replace(tmp, dest)
            self.downloaded += 1
            self.bytes += expected_size
        size = dest.stat().st_size
        if size != expected_size:
            raise RuntimeError(f"{key}: got {size} bytes, expected {expected_size}")
        return size, sha256_of(dest)



_HOME = re.compile(r"(?<![A-Za-z0-9])/(?:Users|home)/[A-Za-z0-9_.-]+/")


class Scrubber:
    """Text-level anonymisation applied to every exported JSON document.

    * home directories (`/Users/<name>/repo/...`, `/home/<name>/...`) become `~/...`;
    * annotator user names, display names and e-mail local parts become `annotator_<id>`
      (the identities live only in the annotators table, which is never exported);
    * the bucket name becomes `<bucket>`.
    Common dictionary words are never treated as names, so ordinary prose is untouched.
    """

    def __init__(self, store: Store, people: list[dict]):
        self.store = store
        common = set()
        try:
            with open("/usr/share/dict/words", encoding="utf-8", errors="ignore") as f:
                common = {w.strip().lower() for w in f if w.strip().islower()}
        except OSError:
            pass
        aliases: dict[str, str] = {}
        for p in people:
            tag = f"annotator_{p['id']}"
            parts = [p.get("username"), p.get("display_name"), (p.get("email") or "").split("@")[0]]
            parts += re.split(r"[\s._-]+", p.get("display_name") or "")
            for v in parts:
                if v and len(v) >= 3 and v.lower() not in common:
                    aliases[v] = tag
        self.aliases = sorted(aliases.items(), key=lambda a: -len(a[0]))
        self.hits = {"home_paths": 0, "annotator_names": 0}

    def __call__(self, text: str) -> str:
        text, n = _HOME.subn("~/", text)
        self.hits["home_paths"] += n
        for name, tag in self.aliases:
            text, n = re.subn(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", tag, text, flags=re.IGNORECASE)
            self.hits["annotator_names"] += n
        return self.store.scrub(text)


class Manifest:
    def __init__(self):
        self.files: dict[str, dict] = {}

    def add(self, path: Path, key: str | None, size: int, sha256: str) -> None:
        self.files[rel(path)] = {"path": rel(path), "s3_key": key, "size": size, "sha256": sha256}

    def add_local(self, path: Path) -> None:
        self.add(path, None, path.stat().st_size, sha256_of(path))

    def write(self, path: Path, scrub) -> None:
        rows = [self.files[k] for k in sorted(self.files)]
        write_json(path, {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bucket": BUCKET_PLACEHOLDER,
            "note": "s3_key is the object key within the bucket the file was exported from (null for files "
                    "generated by the export itself). Paths are repo-relative. Verify with scripts/verify_offline_bundle.py.",
            "files": len(rows),
            "bytes": sum(r["size"] for r in rows),
            "entries": rows,
        }, scrub)


def connect(cfg: Config):
    url = cfg.get("database.v2_url")
    if not url:
        sys.exit("database.v2_url is not configured in config/config.yaml")
    # The pooler host: autocommit SELECTs only, never SET / BEGIN / set_session.
    con = psycopg2.connect(url)
    con.autocommit = True
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select current_database()")
    name = cur.fetchone()["current_database"]
    if "SpreadsheetSmith" not in name:
        sys.exit(f"expected the v2 database, got {name}")
    return cur


def rows_of(cur, sql: str, params=()) -> list[dict]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def data_root() -> Path:
    override = os.environ.get("SPREADSHEETSMITH_DATA_ROOT")
    root = Path(override) if override else ROOT / "data"
    return root if root.is_absolute() else ROOT / root


# ----------------------------------------------------------------------------- tasks
def export_tasks(cur, store: Store, data: Path, manifest: Manifest, scrub) -> None:
    tasks = rows_of(cur, "select * from tasks order by id")
    print(f"tasks: {len(tasks)} rows")
    exported = []
    for t in tasks:
        tdir = data / "tasks" / f"task_id={t['id']}"
        s3_keys = {}
        referenced = set()
        prefix = None
        for col, sub in (("task_starting_files", "starting_files"), ("task_solution_files", "solution_files")):
            keys = [store.key(u) for u in (t[col] or [])]
            s3_keys[col] = keys
            local = []
            for key in keys:
                referenced.add(key)
                prefix = prefix or key.rsplit("/", 2)[0] + "/"
                dest = tdir / sub / PurePosixPath(key).name
                size, digest = store.fetch(key, dest)
                manifest.add(dest, key, size, digest)
                local.append(rel(dest))
            t[col] = local
        # Anything else under the task's object prefix (questions sheets, notes, ...).
        extras = []
        if prefix:
            for key, meta in sorted(store.listing(prefix).items()):
                if key in referenced or key.endswith("/"):
                    continue
                dest = tdir / "extra" / key[len(prefix):]
                size, digest = store.fetch(key, dest, meta["size"])
                manifest.add(dest, key, size, digest)
                extras.append(rel(dest))
        t["s3_keys"] = s3_keys
        if extras:
            t["extra_files"] = extras
        write_json(tdir / "task.json", t, scrub)
        manifest.add_local(tdir / "task.json")
        exported.append(t)
        print(f"  task {t['id']:>3} {t['task_name']:<28} {len(s3_keys['task_starting_files'])} starting, "
              f"{len(s3_keys['task_solution_files'])} solution, {len(extras)} extra")
    write_json(data / "tasks" / "tasks.json", exported, scrub)
    manifest.add_local(data / "tasks" / "tasks.json")


# ----------------------------------------------------------------------------- results
def load_cohorts(data: Path) -> dict:
    return json.loads((data / "results" / "cohorts.json").read_text())


def cohort_pv(c: dict, cohorts: dict) -> int:
    return c["prompt_version"] or cohorts["latest_prompt_version_by_type"][c["agent_model_type"]]


def attempt_dest(attempts_dir: Path, attempt_id: int, key: str, prompt: bool) -> Path:
    name = PurePosixPath(key).name
    return attempts_dir / str(attempt_id) / ("prompts" if prompt else "") / name


def export_results(cur, store: Store, data: Path, manifest: Manifest, attempt_files: str, scrub) -> dict:
    cohorts = load_cohorts(data)
    results = data / "results"
    attempts_dir = results / "attempts"

    # 1. every non-deprecated attempt of every cohort label on its prompt version (failed rows included:
    #    the pointer manifests list their ids under missing_tasks).
    attempts: dict[int, dict] = {}
    for c in cohorts["cohorts"] + cohorts["stage5_cohorts"]:
        for r in rows_of(cur, """
            select * from task_attempts
             where agent_model_type = %s and agent_model_name = any(%s) and prompt_version = %s and not deprecated
             order by id""", (c["agent_model_type"], c["agent_model_names"], cohort_pv(c, cohorts))):
            attempts[r["id"]] = r
    n_cohort = len(attempts)
    # 2. attempts behind a human annotation (the judge-agreement figures join through them).
    annotated = rows_of(cur, "select * from judge_annotations order by id")
    extra_ids = sorted({a["attempt_id"] for a in annotated} - set(attempts))
    for r in rows_of(cur, "select * from task_attempts where id = any(%s) order by id", (extra_ids,)):
        attempts[r["id"]] = r
    print(f"task_attempts: {n_cohort} cohort rows + {len(attempts) - n_cohort} annotated rows")

    # 3. every live grading of those attempts (all judge versions and graders: the pointer manifests
    #    count the other versions/graders), plus every grading behind an annotation.
    gradings: dict[int, dict] = {}
    ids = sorted(attempts)
    for i in range(0, len(ids), 500):
        for g in rows_of(cur, """
            select * from gradings
             where attempt_id = any(%s) and coalesce(deprecated, false) = false and coalesce(failed, false) = false
             order by id""", (ids[i:i + 500],)):
            gradings[g["id"]] = g
    ann_gids = sorted({a["grading_id"] for a in annotated} - set(gradings))
    for g in rows_of(cur, "select * from gradings where id = any(%s) order by id", (ann_gids,)):
        gradings[g["id"]] = g
    print(f"gradings: {len(gradings)} rows ({len(ann_gids)} annotated rows outside the live set)")

    # 4. rewrite paths; fetch the sidecar artifacts if asked; record every artifact's size / etag.
    listing = {}
    for root in sorted({store.key(u).split("/", 2)[0] + "/" + store.key(u).split("/", 2)[1] + "/"
                        for a in attempts.values()
                        for u in list(a["attempt_files"] or []) + list(a["prompt_files"] or [])}):
        listing.update(store.listing(root))
    artifacts: list[dict] = []
    for a in attempts.values():
        s3_keys = {"attempt_files": [store.key(u) for u in (a["attempt_files"] or [])],
                   "prompt_files": [store.key(u) for u in (a["prompt_files"] or [])]}
        first_excel = next((k for k in s3_keys["attempt_files"] if PurePosixPath(k).suffix.lower() in EXCEL_EXTS), None)
        for col, prompt in (("attempt_files", False), ("prompt_files", True)):
            local = []
            for key in s3_keys[col]:
                dest = attempt_dest(attempts_dir, a["id"], key, prompt)
                meta = listing.get(key)
                entry = {"attempt_id": a["id"], "path": rel(dest), "s3_key": key,
                         "size": meta["size"] if meta else None, "etag": meta["etag"] if meta else None,
                         "sha256": None, "role": "workbook" if key == first_excel else ("prompt" if prompt else "artifact")}
                want = attempt_files == "all" or (attempt_files == "workbooks" and key == first_excel)
                if want and meta:
                    _, entry["sha256"] = store.fetch(key, dest, meta["size"])
                elif dest.exists() and meta and dest.stat().st_size == meta["size"]:
                    entry["sha256"] = sha256_of(dest)
                artifacts.append(entry)
                local.append(rel(dest))
            a[col] = local
        a["s3_keys"] = s3_keys
    for g in gradings.values():
        key = store.key(g["raw_files_path"]) if g["raw_files_path"] else None
        g["s3_keys"] = {"raw_files_path": key}
        g["raw_files_path"] = rel(results / "gradings" / str(g["id"])) + "/" if key else None

    write_jsonl(results / "task_attempts.jsonl", [attempts[i] for i in sorted(attempts)], scrub)
    write_jsonl_gz(results / "gradings.jsonl.gz", [gradings[i] for i in sorted(gradings)], scrub)
    manifest.add_local(results / "task_attempts.jsonl")
    manifest.add_local(results / "gradings.jsonl.gz")
    fetched = sum(1 for e in artifacts if e["sha256"])
    write_json(results / "ATTEMPT_FILES_MANIFEST.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Every artifact of every bundled attempt, under data/results/attempts/<attempt_id>/ "
                "(prompt files under prompts/). The folder is a sidecar archive, not tracked in git; "
                "sha256 is filled in for the files that have been fetched, etag/size for all. "
                "role=workbook marks the file the judge grades (the first Excel file listed).",
        "attempts": len(attempts), "files": len(artifacts), "files_with_sha256": fetched,
        "bytes": sum(e["size"] or 0 for e in artifacts),
        "entries": artifacts,
    }, scrub)
    manifest.add_local(results / "ATTEMPT_FILES_MANIFEST.json")
    print(f"attempt artifacts: {len(artifacts)} files, {sum(e['size'] or 0 for e in artifacts) / 1e9:.1f} GB, "
          f"{fetched} with sha256 ({attempt_files})")
    return {"attempts": attempts, "gradings": gradings, "annotations": annotated}


# ----------------------------------------------------------------------------- annotations
def export_annotations(store: Store, data: Path, manifest: Manifest, annotations: list[dict], scrub) -> None:
    """judge_annotations rows + their bundles, with annotator identities reduced to ids."""
    out = data / "results" / "annotations"
    for a in annotations:
        key = a.get("s3_key")
        dest = None
        if key:
            stamp = a["created_at"].strftime("%Y%m%dT%H%M%SZ")
            dest = out / f"grading_id={a['grading_id']}" / f"annotator_{a['annotator_id']}_rev{a['revision']}_{stamp}.json"
            raw = store.s3.get_object(Bucket=store.bucket, Key=store.key(key))["Body"].read()
            body = scrub(json.dumps(json.loads(raw.decode("utf-8")), indent=2, ensure_ascii=False)) + "\n"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
            manifest.add_local(dest)
        a["s3_key"] = rel(dest) if dest else None
        a["s3_keys"] = {"s3_key": "withheld: the original key carried the annotator's name"}
    write_jsonl(data / "results" / "judge_annotations.jsonl", annotations, scrub)
    manifest.add_local(data / "results" / "judge_annotations.jsonl")
    print(f"judge_annotations: {len(annotations)} rows, {sum(1 for a in annotations if a['s3_key'])} bundles")


# ----------------------------------------------------------------------------- experiments
def export_experiments(data: Path, manifest: Manifest, attempts: dict, gradings: dict, scrub) -> None:
    cohorts = load_cohorts(data)
    graders = set(cohorts["leaderboard_graders"])
    jv = cohorts["leaderboard_judge_version"]
    tasks = {t["id"]: t for t in json.loads((data / "tasks" / "tasks.json").read_text())}
    live = [g for g in gradings.values() if g["judge_version"] == jv and g["attempt_id"] in attempts]
    sol_by_attempt: dict[int, list[dict]] = {}
    fable_by_attempt: dict[int, list[dict]] = {}
    for g in sorted(live, key=lambda g: g["id"]):
        (sol_by_attempt if g["grader_model"] in graders else fable_by_attempt).setdefault(g["attempt_id"], []).append(g)

    def label_of(c):
        return c["agent_model_names"]

    def cohort(pipeline, model):
        return next(c for c in cohorts["cohorts"] if c["pipeline"] == pipeline and c["model"] == model)

    roles: dict[int, list[tuple[str, str]]] = {}

    def tag(gid, stage, role):
        roles.setdefault(gid, []).append((stage, role))

    # stage 2: attempts with three or more Sol gradings.
    stage2_attempts = sorted(a for a, gs in sol_by_attempt.items() if len(gs) >= 3)
    for a in stage2_attempts:
        gs = sol_by_attempt[a]
        tag(gs[0]["id"], "2", "stage2:sol_production")
        for g in gs[1:]:
            tag(g["id"], "2", "stage2:sol_repeat")
    # stage 3: attempts with a non-Sol (Fable) judge grading, next to their production grading.
    stage3_attempts = sorted(a for a in fable_by_attempt if a in sol_by_attempt)
    for a in stage3_attempts:
        tag(sol_by_attempt[a][0]["id"], "3", "stage3:sol_production")
        for g in fable_by_attempt[a]:
            tag(g["id"], "3", "stage3:fable_judge")
    # stage 4 / 5: the production grading of each cohort arm.
    arms = {"4": {"stage4:effort_ablation_low": cohort("coding", "fable_low"),
                  "stage4:effort_ablation_high": cohort("coding", "fable_high"),
                  "stage4:effort_ablation_max": cohort("coding", "fable")},
            "5": {"stage5:fable_max_v13_claudecode": cohort("coding", "fable"),
                  "stage5:fable_max_v14_codex": cohort("coding", "fable_v14"),
                  "stage5:fable_max_v15_codex": cohort("coding", "fable_v15")}}
    for stage, by_role in arms.items():
        for role, c in by_role.items():
            pv = cohort_pv(c, cohorts)
            for a, gs in sol_by_attempt.items():
                att = attempts[a]
                if att["agent_model_name"] in label_of(c) and att["prompt_version"] == pv and not att["agent_failed"]:
                    tag(gs[0]["id"], stage, role)

    def effort(label: str):
        m = re.search(r"-(low|medium|high|xhigh|max)$", label)
        return m.group(1) if m else None

    out = []
    for gid in sorted(roles):
        g = gradings[gid]
        att = attempts[g["attempt_id"]]
        t = tasks[att["task_id"]]
        out.append({
            "grading_id": gid, "attempt_id": g["attempt_id"], "task_id": att["task_id"], "task_name": t["task_name"],
            "task_difficulty": t["human_difficulty_measure"], "agent_model_name": att["agent_model_name"],
            "agent_effort": effort(att["agent_model_name"]), "prompt_version": att["prompt_version"],
            "judge_model": g["grader_model"], "judge_version": g["judge_version"],
            "total_score": (g["scored_results"] or {}).get("total_score"),
            "stages": [s for s, _ in roles[gid]], "stage_roles": [r for _, r in roles[gid]],
        })
    exp = data / "results" / "experiments"
    write_json(exp / "consolidated_gradings_stages_2_3_4.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": STAGES_NOTE, "judge_version": jv, "gradings": out}, scrub)
    write_json(exp / "stage2_repeat_attempt_ids.json", {
        "note": "The 90 attempts the leaderboard judge graded three times (stage 2, judge consistency). "
                "Re-run: grade each attempt twice more with the leaderboard judge configuration.",
        "attempt_ids": stage2_attempts}, scrub)
    write_json(exp / "stage3_second_judge_attempt_ids.json", {
        "note": "The attempts graded by the second judge next to the leaderboard judge (stage 3). "
                "Re-run: grade each attempt once with the second judge identity.",
        "judge_models": sorted({g["grader_model"] for gs in fable_by_attempt.values() for g in gs}),
        "attempt_ids": stage3_attempts}, scrub)
    for p in ("consolidated_gradings_stages_2_3_4.json", "stage2_repeat_attempt_ids.json", "stage3_second_judge_attempt_ids.json"):
        manifest.add_local(exp / p)
    counts = {}
    for rs in roles.values():
        for _, r in rs:
            counts[r] = counts.get(r, 0) + 1
    print("experiments:", json.dumps(counts))


# ----------------------------------------------------------------------------- toys
def export_toys(cur, store: Store, data: Path, manifest: Manifest, toy_files: bool, scrub) -> None:
    jr = data / "judge_reliability"
    tasks = rows_of(cur, "select * from judge_reliability.toy_tasks order by id")
    runs = rows_of(cur, "select * from judge_reliability.toy_runs order by started_at, run_id")
    grads = rows_of(cur, "select * from judge_reliability.toy_gradings order by id")
    sidecar = []
    for t in tasks:
        t["s3_keys"] = {}
        for col in ("s3_pass", "s3_fail"):
            key = store.key(t[col]) if t[col] else None
            t["s3_keys"][col] = key
            if not key:
                continue
            variant = "Pass" if col == "s3_pass" else "Fail"
            dest = jr / "toy_tasks" / t["folder"] / variant / PurePosixPath(key).name
            expected = t["bytes_pass" if col == "s3_pass" else "bytes_fail"]
            entry = {"toy_task_id": t["id"], "path": rel(dest), "s3_key": key, "size": expected,
                     "sha256": t["sha256_pass" if col == "s3_pass" else "sha256_fail"], "fetched": False}
            if toy_files:
                _, digest = store.fetch(key, dest, expected)
                if entry["sha256"] and digest != entry["sha256"]:
                    raise RuntimeError(f"toy {t['id']} {variant}: sha256 mismatch against the toy_tasks row")
                entry["sha256"], entry["fetched"] = digest, True
            elif dest.exists():
                entry["fetched"] = dest.stat().st_size == expected
            sidecar.append(entry)
            t[col] = rel(dest)
    for g in grads:
        key = store.key(g["raw_files_path"]) if g.get("raw_files_path") else None
        g["s3_keys"] = {"raw_files_path": key}
        g["raw_files_path"] = rel(jr / "gradings" / g["run_id"] / str(g["id"])) + "/" if key else None
    write_jsonl(jr / "toy_tasks.jsonl", tasks, scrub)
    write_jsonl(jr / "toy_runs.jsonl", runs, scrub)
    write_jsonl(jr / "toy_gradings.jsonl", grads, scrub)
    write_json(jr / "TOY_FILES_MANIFEST.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "The toy workbooks under data/judge_reliability/toy_tasks/ (sidecar, not tracked in git). "
                "sha256 and size come from the toy_tasks rows; fetched says whether this export pulled the file.",
        "files": len(sidecar), "bytes": sum(e["size"] or 0 for e in sidecar), "entries": sidecar}, scrub)
    for p in ("toy_tasks.jsonl", "toy_runs.jsonl", "toy_gradings.jsonl", "TOY_FILES_MANIFEST.json"):
        manifest.add_local(jr / p)
    print(f"judge_reliability: {len(tasks)} toy tasks, {len(runs)} runs, {len(grads)} gradings, "
          f"{sum(e['size'] or 0 for e in sidecar) / 1e9:.2f} GB of toy workbooks ({'fetched' if toy_files else 'not fetched'})")


# ----------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", choices=["tasks", "results", "annotations", "experiments", "toys"],
                    help="export only these sections (default: all)")
    ap.add_argument("--attempt-files", choices=["none", "workbooks", "all"], default="none",
                    help="fetch the sidecar attempt artifacts: the judged workbooks (~7 GB) or everything (~21 GB)")
    ap.add_argument("--toy-files", action="store_true", help="fetch the toy workbooks (~1 GB sidecar)")
    args = ap.parse_args()
    only = set(args.only or ["tasks", "results", "annotations", "experiments", "toys"])

    cfg = Config.load(create_missing=False, check_required=False)
    store = Store(cfg)
    cur = connect(cfg)
    scrub = Scrubber(store, rows_of(cur, "select id, username, display_name, email from annotators"))
    data = data_root()
    manifest = Manifest()
    existing = data / "MANIFEST.json"
    if existing.exists():  # keep entries of sections not re-exported this time
        for e in json.loads(existing.read_text())["entries"]:
            manifest.files[e["path"]] = e
    t0 = time.time()

    if "tasks" in only:
        export_tasks(cur, store, data, manifest, scrub)
    res = None
    if {"results", "annotations", "experiments"} & only:
        res = export_results(cur, store, data, manifest, args.attempt_files, scrub)
    if "annotations" in only:
        export_annotations(store, data, manifest, res["annotations"], scrub)
    if "experiments" in only:
        export_experiments(data, manifest, res["attempts"], res["gradings"], scrub)
    if "toys" in only:
        export_toys(cur, store, data, manifest, args.toy_files, scrub)

    # Drop manifest entries whose file is gone, then re-verify sizes of everything listed.
    for path in list(manifest.files):
        if not (ROOT / path).exists():
            del manifest.files[path]
    manifest.write(data / "MANIFEST.json", scrub)
    print(f"scrubbed: {scrub.hits}")
    print(f"MANIFEST.json: {len(manifest.files)} files, {sum(e['size'] for e in manifest.files.values()) / 1e6:.1f} MB; "
          f"downloaded {store.downloaded} objects / {store.bytes / 1e6:.1f} MB in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
