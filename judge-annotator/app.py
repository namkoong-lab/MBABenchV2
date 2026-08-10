"""Judge Annotation Site — internal tool for auditing LLM-judge grades.

Flow: login -> browse gradings (filter model/pipeline/task/judge) -> annotate
each rubric check (agree/disagree + note) -> derived TP/TN/FP/FN persisted to
S3 (full JSON) + one pointer row in Neon `judge_annotations`.
"""
import json
import os
import threading
import time
from collections import defaultdict, deque

import bcrypt
from datetime import datetime, timezone
from pathlib import Path

import boto3
import psycopg2
import psycopg2.extras
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

BASE = Path(__file__).parent
CFG = {
    "DATABASE_URL": os.environ["DATABASE_URL"],
    "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
    "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
    "SECRET_KEY": os.environ["SECRET_KEY"],
}
ANNOTATION_BUCKET = "mbabench"
ANNOTATION_PREFIX = "annotations/"
# Frontier-wave identities: attempts by these agents become candidates as soon
# as they have usable gradings (no allowlist rebuild needed).
FRONTIER_IDENTITIES = [
    "openpyxl_anthropic/claude-fable-5-max",
    "openpyxl_openai/gpt-5.6-sol-xhigh",
    "claude_web_cowork_fable5_max",
    "chatgpt_web_work_gpt5.6_sol_ultra",
    # Coding-agent pipeline (added 2026-08-06)
    "claudecode_anthropic/claude-fable-5-max",
    "codex_openai/gpt-5.6-sol-xhigh",
]
QUEUE_TTL_SEC = 600

USERS = json.loads((BASE / "users.json").read_text())  # {username: bcrypt hash}
SESSION_MAX_AGE = 12 * 3600
LOGIN_WINDOW_SEC = 900
LOGIN_MAX_TRIES = 8
_login_hits: dict = defaultdict(deque)
_login_lock = threading.Lock()


def rate_limited(ip: str) -> bool:
    """Per-IP sliding window over failed logins; blocks password brute force."""
    now = time.time()
    with _login_lock:
        hits = _login_hits[ip]
        while hits and now - hits[0] > LOGIN_WINDOW_SEC:
            hits.popleft()
        return len(hits) >= LOGIN_MAX_TRIES


def record_failure(ip: str):
    with _login_lock:
        _login_hits[ip].append(time.time())

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE / "templates"))
signer = URLSafeSerializer(CFG["SECRET_KEY"], salt="session")
# Attempt/grading artifacts span two buckets in two regions (mbabench us-east-2,
# biz-bench us-east-1). A presigned URL is only valid if signed for the bucket's
# own region, so clients are built per region and cached.
def _client(region: str):
    return boto3.client(
        "s3",
        aws_access_key_id=CFG["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=CFG["AWS_SECRET_ACCESS_KEY"],
        region_name=region,
        config=BotoConfig(signature_version="s3v4"),
    )


_probe = _client("us-east-1")
_region_cache: dict = {}
_client_cache: dict = {"us-east-1": _probe}


def bucket_region(bucket: str) -> str:
    if bucket not in _region_cache:
        try:
            hdrs = _probe.head_bucket(Bucket=bucket)["ResponseMetadata"]["HTTPHeaders"]
        except ClientError as e:
            hdrs = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        _region_cache[bucket] = hdrs.get("x-amz-bucket-region") or "us-east-1"
    return _region_cache[bucket]


def s3_for(bucket: str):
    region = bucket_region(bucket)
    if region not in _client_cache:
        _client_cache[region] = _client(region)
    return _client_cache[region]


def db():
    return psycopg2.connect(CFG["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)


def pipeline_of(agent: str) -> str:
    if agent.startswith(("claudecode_", "codex_")):
        return "Coding agent"
    if agent.startswith("openpyxl"):
        return "CLI/API"
    if "excel_agent" in agent:
        return "Excel add-in"
    return "GUI"


def usable(scored_results) -> bool:
    try:
        sr = scored_results if isinstance(scored_results, dict) else json.loads(scored_results)
        warn = sr.get("scoring_warnings", {}) or {}
        return not warn.get("unscored_checks")
    except Exception:
        return False


# ---------------------------------------------------------------- queue cache
_queue_lock = threading.Lock()
_queue: dict = {"rows": [], "loaded_at": 0.0}


def candidate_attempt_ids(cur):
    cand = json.loads((BASE / "candidates.json").read_text())["rows"]
    ids = {r["attempt_id"] for r in cand}
    corrected = {r["attempt_id"] for r in cand if r.get("corrected")}
    cur.execute(
        """SELECT DISTINCT ta.id FROM task_attempts ta
           WHERE ta.agent_model_name = ANY(%s) AND ta.deprecated IS NOT TRUE""",
        (FRONTIER_IDENTITIES,),
    )
    ids |= {r["id"] for r in cur.fetchall()}
    return ids, corrected


# v1-era boundary: gradings before this are "the leaderboard originals"; only the
# latest usable one per (attempt, judge) counts, and none count for attempts whose
# v1 grade was invalidated and regraded (candidates.json corrected=true).
V1_CUTOFF = datetime(2026, 6, 1, tzinfo=timezone.utc)
V1_PROMPT_VERSIONS = ("7.0", "4")


def load_queue(force=False):
    with _queue_lock:
        if not force and _queue["rows"] and time.time() - _queue["loaded_at"] < QUEUE_TTL_SEC:
            return _queue["rows"]
        conn = db()
        cur = conn.cursor()
        ids, corrected = candidate_attempt_ids(cur)
        cur.execute(
            """SELECT g.id AS grading_id, g.attempt_id, g.grader_model, g.created_at,
                      g.prompt_version, g.scored_results, g.raw_files_path,
                      ta.agent_model_name AS agent, ta.task_id, t.task_name, t.task_source
               FROM gradings g
               JOIN task_attempts ta ON ta.id = g.attempt_id
               JOIN tasks t ON t.id = ta.task_id
               WHERE g.attempt_id = ANY(%s) AND g.deprecated IS NOT TRUE
                 AND ta.deprecated IS NOT TRUE
                 AND g.raw_files_path IS NOT NULL
               ORDER BY t.task_name, g.attempt_id, g.id""",
            (list(ids),),
        )
        raw = [r for r in cur.fetchall() if usable(r["scored_results"])]
        # Pre-cutoff (v1-era): keep only the latest per (attempt, judge) among the
        # final prompt versions, and drop entirely for regraded (corrected) attempts.
        # Post-cutoff rows are deliberate grades (regrades, other judges, repeats): keep all.
        latest_v1: dict = {}
        for r in raw:
            if r["created_at"] < V1_CUTOFF:
                if r["attempt_id"] in corrected or str(r["prompt_version"]) not in V1_PROMPT_VERSIONS:
                    continue
                k = (r["attempt_id"], r["grader_model"])
                if k not in latest_v1 or r["grading_id"] > latest_v1[k]["grading_id"]:
                    latest_v1[k] = r
        keep = list(latest_v1.values()) + [r for r in raw if r["created_at"] >= V1_CUTOFF]
        keep.sort(key=lambda r: (r["task_name"], r["attempt_id"], r["grading_id"]))
        rows = []
        for r in keep:
            sr = r["scored_results"]
            sr = sr if isinstance(sr, dict) else json.loads(sr)
            rows.append(
                {
                    "grading_id": r["grading_id"],
                    "attempt_id": r["attempt_id"],
                    "task_id": r["task_id"],
                    "task_name": r["task_name"],
                    "task_source": r["task_source"],
                    "agent": r["agent"],
                    "pipeline": pipeline_of(r["agent"]),
                    "judge": r["grader_model"],
                    "graded_at": r["created_at"].strftime("%Y-%m-%d"),
                    "total": round(sr.get("total_score") or 0, 1),
                }
            )
        cur.execute("SELECT grading_id, annotator FROM judge_annotations")
        seen: dict = {}
        for a in cur.fetchall():
            seen.setdefault(a["grading_id"], set()).add(a["annotator"])
        for row in rows:
            row["annotators"] = sorted(seen.get(row["grading_id"], []))
        conn.close()
        _queue.update(rows=rows, loaded_at=time.time())
        return rows


# ---------------------------------------------------------------- auth
def current_user(request: Request):
    tok = request.cookies.get("session")
    if not tok:
        return None
    try:
        data = signer.loads(tok)
    except BadSignature:
        return None
    if time.time() - data.get("t", 0) > SESSION_MAX_AGE:
        return None
    return data["u"]


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?")
    if rate_limited(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many failed attempts. Try again in 15 minutes."}, status_code=429)
    stored = USERS.get(username, "")
    ok = bool(stored) and bcrypt.checkpw(password.encode(), stored.encode())
    if ok:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("session", signer.dumps({"u": username, "t": time.time()}),
                        httponly=True, secure=True, samesite="lax", max_age=SESSION_MAX_AGE)
        return resp
    record_failure(ip)
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid credentials"})


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ---------------------------------------------------------------- browse
@app.get("/", response_class=HTMLResponse)
def browse(request: Request, model: str = "", pipeline: str = "", task: str = "",
           judge: str = "", status: str = "", refresh: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    rows = load_queue(force=bool(refresh))
    models = sorted({r["agent"] for r in rows})
    judges = sorted({r["judge"] for r in rows})
    pipelines = sorted({r["pipeline"] for r in rows})
    if model:
        rows = [r for r in rows if r["agent"] == model]
    if pipeline:
        rows = [r for r in rows if r["pipeline"] == pipeline]
    if judge:
        rows = [r for r in rows if r["judge"] == judge]
    if task:
        rows = [r for r in rows if task.lower() in r["task_name"].lower()]
    if status == "unreviewed":
        rows = [r for r in rows if not r["annotators"]]
    elif status == "reviewed":
        rows = [r for r in rows if r["annotators"]]
    return templates.TemplateResponse(request, "browse.html", {
        "user": user, "rows": rows[:500], "n_total": len(rows),
        "models": models, "judges": judges, "pipelines": pipelines,
        "f": {"model": model, "pipeline": pipeline, "task": task, "judge": judge, "status": status},
    })


# ---------------------------------------------------------------- annotate
def canonical(uri: str) -> str:
    """All artifacts live in mbabench; rewrite legacy biz-bench URIs."""
    return uri.replace("s3://biz-bench/", f"s3://{ANNOTATION_BUCKET}/", 1)


def presign(uri: str, expires=3600) -> str:
    bucket, key = canonical(uri).replace("s3://", "").split("/", 1)
    return s3_for(bucket).generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
    )


def fetch_judgement(raw_files_path: str) -> dict:
    # Legacy rows point at the retired biz-bench bucket; every one of those bundles
    # was mirrored into mbabench under the identical key (verified 2026-08-05,
    # 1824/1824), so all reads go to mbabench.
    bucket, key = canonical(raw_files_path).replace("s3://", "").split("/", 1)
    obj = s3_for(bucket).get_object(Bucket=bucket, Key=key.rstrip("/") + "/ai_judgement.json")
    return json.loads(obj["Body"].read())


def grading_context(grading_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """SELECT g.id AS grading_id, g.attempt_id, g.grader_model, g.created_at,
                  g.prompt_version, g.raw_files_path, g.scored_results,
                  ta.agent_model_name AS agent, ta.attempt_files, ta.task_id,
                  t.task_name, t.task_source, t.task_starting_files, t.task_solution_files
           FROM gradings g
           JOIN task_attempts ta ON ta.id = g.attempt_id
           JOIN tasks t ON t.id = ta.task_id
           WHERE g.id = %s
             AND g.deprecated IS NOT TRUE AND ta.deprecated IS NOT TRUE""",
        (grading_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


@app.get("/grade/{grading_id}", response_class=HTMLResponse)
def grade_page(request: Request, grading_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    row = grading_context(grading_id)
    if not row:
        return HTMLResponse("Grading not found", status_code=404)
    judgement = fetch_judgement(row["raw_files_path"])
    dims = []
    for dim in ("Accuracy", "Formula", "Formatting"):
        checks = judgement.get(dim) or []
        dims.append({"name": dim, "checks": checks})
    files = {
        "attempt": [(u.rsplit("/", 1)[-1], presign(u)) for u in (row["attempt_files"] or [])
                    if u.endswith((".xlsx", ".xlsm"))],
        "solution": [(u.rsplit("/", 1)[-1], presign(u)) for u in (row["task_solution_files"] or [])],
        "task": [(u.rsplit("/", 1)[-1], presign(u)) for u in (row["task_starting_files"] or [])],
    }
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT annotator, created_at FROM judge_annotations WHERE grading_id=%s "
                "ORDER BY created_at", (grading_id,))
    prior = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(request, "grade.html", {
        "user": user, "g": row, "dims": dims, "files": files, "prior": prior,
    })


@app.post("/grade/{grading_id}")
async def grade_submit(request: Request, grading_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    row = grading_context(grading_id)
    if not row:
        return HTMLResponse("Grading not found or deprecated — not annotatable.", status_code=404)
    judgement = fetch_judgement(row["raw_files_path"])
    form = await request.form()
    labels, detail = {}, []
    for dim in ("Accuracy", "Formula", "Formatting"):
        for chk in judgement.get(dim) or []:
            key = f"{dim}::{chk.get('name') or chk.get('check')}"
            verdict = form.get(key)  # "agree" | "disagree" | None
            note = (form.get(key + "::note") or "").strip()
            if verdict not in ("agree", "disagree"):
                continue
            failed = chk.get("decision") == "fail"
            label = {(True, "agree"): "TP", (True, "disagree"): "FP",
                     (False, "agree"): "TN", (False, "disagree"): "FN"}[(failed, verdict)]
            labels[key] = label
            detail.append({"dimension": dim, "check": chk.get("name") or chk.get("check"),
                           "judge_decision": chk.get("decision"), "annotator_verdict": verdict,
                           "label": label, "note": note})
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"{ANNOTATION_PREFIX}grading_id={grading_id}/{user}_{ts}.json"
    payload = {
        "grading_id": grading_id, "attempt_id": row["attempt_id"], "task_id": row["task_id"],
        "task_name": row["task_name"], "agent": row["agent"], "judge": row["grader_model"],
        "annotator": user, "created_at": ts, "checks": detail,
        "overall_note": (form.get("overall_note") or "").strip(),
    }
    s3_for(ANNOTATION_BUCKET).put_object(
        Bucket=ANNOTATION_BUCKET, Key=s3_key,
        Body=json.dumps(payload, indent=1).encode(), ContentType="application/json")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO judge_annotations (grading_id, attempt_id, annotator, labels, s3_key)
           VALUES (%s, %s, %s, %s, %s)""",
        (grading_id, row["attempt_id"], user, json.dumps(labels), s3_key),
    )
    conn.commit()
    conn.close()
    load_queue(force=True)
    return RedirectResponse("/?status=", status_code=303)
