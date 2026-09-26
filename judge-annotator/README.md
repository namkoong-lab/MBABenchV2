# Judge Annotator

> **Not needed to reproduce the benchmark.** This is the operational web app
> the human review was carried out in, not a step in the pipeline. Its output (the
> human annotation rows) is not distributed with this repository. Nothing outside this directory
> imports it — `git grep -i judge.annotator` finds only a line in the top-level
> README — and it is the one component here that still has no offline mode.
>
> That is deliberate rather than pending. The app is v1-era: it renders the
> three v1 rubric categories (`Accuracy`, `Formula`, `Formatting`), so it would
> show an empty page for a v2 grading, whose judgements are keyed by rubric_9's
> twelve. Its queue is database-shaped throughout — a seed file intersected
> with live attempt rows, a cutoff date, a join against the annotations table —
> and its file links are presigned object-store URLs. A `--local` flag would
> therefore not be a flag: the queue, the grade page, its templates and the
> label derivation would all have to be rewritten, to rebuild a tool whose
> results are already in the bundle. If the human review is ever repeated, that
> rewrite is the right moment for it.

Web app for human annotation of SpreadsheetSmith judge gradings. Reviewers see each
grading's per-check verdicts (from the bundle `ai_judgement.json`), download the
attempt workbook / golden solution / task files via presigned S3 URLs, and mark
agree/disagree with a note per check (derived TP/TN/FP/FN).

- **Live:** a single small cloud VM with a static IP, reached over HTTPS at
  `https://<ip>.sslip.io` (the address is held with the deployment
  credentials, not in git)
- **Stack:** FastAPI + Jinja2 behind Caddy (auto-HTTPS via sslip.io), run by
  systemd. No local database — the app is stateless.
- **Reads:** Neon `BizbenchV1` (`gradings`, `task_attempts`, `tasks`) via the
  restricted `annotator_app` role; grading bundles from `s3://<bucket>`
  (presigned URLs must be signed for the bucket's own region).
- **Writes:** annotation JSON to `s3://<bucket>/annotations/grading_id=<id>/<user>_<ts>.json`
  plus a pointer row in Neon `judge_annotations` (the only table it inserts into).
- **Queue:** candidate attempt ids ship in `candidates.json`
  (= `raw_grade_data_corrected.json` from the 2026-08 v1 regrade) ∪ frontier-wave
  identities, with pre-/post-2026-06 grading filters implemented in `app.py`.

## Layout

| File | Purpose |
|---|---|
| `app.py` | the whole app (routes, auth, queue rules, S3/DB access) |
| `templates/` | `login`, `browse`, `grade` pages |
| `candidates.json` | annotation queue seed data (non-secret) |
| `users.json.example` | shape of the real `users.json` (bcrypt hashes; NOT in git) |
| `.env.example` | env vars the service needs (real values live only on the server), including `ANNOTATION_BUCKET` — the bucket is read from the environment, never hardcoded, so an unset variable fails on the first S3 call rather than writing somewhere unintended |
| `deploy/judge-annotator.service` | systemd unit (as deployed) |
| `deploy/Caddyfile` | reverse-proxy + HTTPS config (as deployed) |

## Rebuild from scratch (fresh Lightsail/any Ubuntu box)

1. Create an Ubuntu instance (small, $12/mo tier is enough), attach a static IP,
   open ports 80/443.
2. Copy this directory to `~/judge-annotator` on the box.
3. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
4. Create `/etc/judge-annotator.env` (mode 600) from `.env.example` — use your
   own Neon `annotator_app` password and a least-privilege AWS key.
5. Create `users.json` from the example (one bcrypt hash per user).
6. Install the systemd unit: copy `deploy/judge-annotator.service` to
   `/etc/systemd/system/`, `sudo systemctl enable --now judge-annotator`.
7. `sudo apt install caddy`, put `deploy/Caddyfile` at `/etc/caddy/Caddyfile`
   with the new static IP in the hostname, `sudo systemctl reload caddy`.
8. Done — `https://<new-ip>.sslip.io`. No data migration needed: all annotation
   state lives in Neon + S3.

## Deploy an edit to the running site

```bash
rsync -avz -e "ssh -i <key>" --exclude .venv --exclude __pycache__ \
  ./ ubuntu@<host>:~/judge-annotator/
ssh -i <key> ubuntu@<host> 'sudo systemctl restart judge-annotator'
```

## Operational notes

- Login is rate-limited (8 failures / 15 min / IP, in-memory) — locking yourself
  out during testing is cleared by a service restart. Sessions expire after 12 h.
- The server's AWS key should be scoped to `s3://<bucket>` read + `annotations/`
  write (a drafted `iam_policy.json` existed; recreate before granting new keys).
- `users.json` and `/etc/judge-annotator.env` are deliberately not in git —
  hand them over via a password manager when transferring ownership.
