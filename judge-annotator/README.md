# Judge Annotator

Web app for human annotation of MBABench judge gradings. Reviewers see each
grading's per-check verdicts (from the bundle `ai_judgement.json`), download the
attempt workbook / golden solution / task files via presigned S3 URLs, and mark
agree/disagree with a note per check (derived TP/TN/FP/FN).

- **Live:** https://54.84.197.221.sslip.io (AWS Lightsail `judge-annotator`,
  us-east-1a, $12/mo, static IP `judge-annotator-ip`)
- **Stack:** FastAPI + Jinja2 behind Caddy (auto-HTTPS via sslip.io), run by
  systemd. No local database — the app is stateless.
- **Reads:** Neon `BizbenchV1` (`gradings`, `task_attempts`, `tasks`) via the
  restricted `annotator_app` role; grading bundles from `s3://mbabench`
  (us-east-2 — presigned URLs must be signed for that region).
- **Writes:** annotation JSON to `s3://mbabench/annotations/grading_id=<id>/<user>_<ts>.json`
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
| `.env.example` | env vars the service needs (real values live only on the server) |
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
rsync -avz -e "ssh -i ~/.ssh/lightsail_default.pem" --exclude .venv --exclude __pycache__ \
  ./ ubuntu@54.84.197.221:~/judge-annotator/
ssh -i ~/.ssh/lightsail_default.pem ubuntu@54.84.197.221 'sudo systemctl restart judge-annotator'
```

## Operational notes

- Login is rate-limited (8 failures / 15 min / IP, in-memory) — locking yourself
  out during testing is cleared by a service restart. Sessions expire after 12 h.
- The server's AWS key should be scoped to `s3://mbabench` read + `annotations/`
  write (a drafted `iam_policy.json` existed; recreate before granting new keys).
- `users.json` and `/etc/judge-annotator.env` are deliberately not in git —
  hand them over via a password manager when transferring ownership.
