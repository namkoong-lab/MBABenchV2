# Dispatcher — common commands

All commands run from the repo root as `python -m infra.dispatcher.dispatch <...>`.
`<alias>` refers to a box alias defined in [boxes.yaml](boxes.yaml).

`status` / `show` / `assign` check whether your current public IP is in the
dispatcher security group before fanning out over SSH. If it isn't, they print
a warning pointing at `aws_bootstrap.sh` — re-run that script (or set
`DISPATCH_NO_DIAGNOSE=1` to suppress the check).

## Spin up / tear down

```bash
# Launch (or re-provision) a box from a config template
bash infra/dispatcher/spinup.sh --alias chatgpt-pro-1 --config-template infra/dispatcher/config_templates/chatgpt_pro.yaml

# Terminate one box by alias
bash infra/dispatcher/teardown.sh --alias chatgpt-pro-1

# Terminate every gui-agents box in the region
bash infra/dispatcher/teardown.sh --all
```

## Inspect boxes

```bash
# One-line state for every box
python -m infra.dispatcher.dispatch status

# Auto-refresh every few seconds
python -m infra.dispatcher.dispatch status -f

# Full state.json for a single box
python -m infra.dispatcher.dispatch show <alias>
```

## Assign tasks

```bash
# Pick N eligible tasks from the DB and fan them out
python -m infra.dispatcher.dispatch assign --n 10

# Filter by agent or task source
python -m infra.dispatcher.dispatch assign --n 10 --agent <agent_model_name>
python -m infra.dispatcher.dispatch assign --n 10 --task-source <source>

# Assign specific task ids
python -m infra.dispatcher.dispatch assign --tasks 42,43,44

# Pin all picks to one box
python -m infra.dispatcher.dispatch assign --n 5 --box <alias>

# Skip the confirmation prompt
python -m infra.dispatcher.dispatch assign --n 5 -y
```

After a successful `assign`, a per-cohort `remaining after this batch: …`
line prints how many eligible tasks are still un-queued — same number
`backlog` would have shown for the same cohort.

## Backlog

Per-cohort count of eligible tasks. For each reachable cohort
`(agent_model_name, prompt_version)`, prints:

- `in_flight` — tasks already in a box's `current` or `queue` across the cohort
- `unassigned` — DB-eligible tasks, minus `in_flight` (what `assign` would pull)
- `remaining` — `in_flight + unassigned` (total work left for this cohort)
- `total` — all DB rows in cohort scope (non-deprecated, matching `--task-source`),
  including ones already successfully attempted — the full universe

`unassigned` and `remaining` use the same filters as `assign`, so they
match what `assign --n ∞` would pull.

```bash
# Backlog across every reachable cohort
python -m infra.dispatcher.dispatch backlog

# Narrow to one cohort
python -m infra.dispatcher.dispatch backlog --agent <agent_model_name>
python -m infra.dispatcher.dispatch backlog --task-source <source>
```

## Cancel / clear

```bash
# Cancel a single queued or running task on a box
python -m infra.dispatcher.dispatch cancel <alias> <task_id>

# Drop the entire pending queue on a box
python -m infra.dispatcher.dispatch clear <alias>
```

## Logs

Pager opens at the bottom (most recent lines) — scroll up for history.

```bash
# Worker service journal for a box
python -m infra.dispatcher.dispatch logs <alias>

# Logs for one task unit
python -m infra.dispatcher.dispatch logs <alias> --task <task_id>

# Live follow
python -m infra.dispatcher.dispatch logs <alias> -f
python -m infra.dispatcher.dispatch logs <alias> --task <task_id> -f
```

## Browser login (VNC)

First-time or session-expired logins to claude.ai / chatgpt.com. Starts x11vnc
on the box's Xvfb display and tunnels it to your laptop. Cookies persist in the
box's Chrome `--user-data-dir`, so the worker picks up the refreshed session.

On macOS, the command auto-opens the built-in Screen Sharing viewer.

```bash
# Default: forwards box:5901 -> localhost:5901, opens vnc://localhost:5901
python -m infra.dispatcher.dispatch login <alias>

# Custom local port (e.g. running two logins in parallel)
python -m infra.dispatcher.dispatch login <alias> --local-port 5902

# Don't auto-launch the VNC viewer
python -m infra.dispatcher.dispatch login <alias> --no-open
```

In the VNC session: log in through the already-running Chrome window, then
Ctrl-C the dispatcher terminal to tear down the tunnel.

## Auth probe

The worker periodically probes claude.ai / chatgpt.com to verify the
browser session is still live. Results show up in the `login` column of
`dispatch status`:

- `<email>` — last probe succeeded and is fresh
- `old <email>` — last probe succeeded but is >30 min old (cookie is
  likely still good, just hasn't been re-verified)
- `STALE` — last probe failed — needs `dispatch login`
- `?` — no probe result yet

The `old` prefix is suppressed while the box has a running task: the
probe oneshot deliberately short-circuits during worker activity (it
would race with the agent over the shared Chrome), so staleness during
that window is expected and un-actionable. The column just shows
`<email>` until the task ends and the next probe runs.

`dispatch probe` kicks the auth-probe oneshot on demand, so `status`
reflects the current login immediately instead of waiting for the next
timer fire. Only useful for `old` entries — `STALE` means the session
is actually broken and needs a re-login via VNC.

```bash
# Refresh one box's login status
python -m infra.dispatcher.dispatch probe <alias>

# Refresh every registered box
python -m infra.dispatcher.dispatch probe --all
```

`dispatch status` will also detect `old` entries after printing the
table and prompt you to probe them in one keystroke — press `y` to
kick probes on all of them, anything else to skip. The prompt is
suppressed when stdin isn't a TTY (piped output, `watch`, scripts).

## Config (box-local configs.yaml)

```bash
# Pull remote config to a temp file; prints the path
python -m infra.dispatcher.dispatch config pull <alias>

# Push a local file as the new remote config
python -m infra.dispatcher.dispatch config push <alias> <localfile>

# Diff the live config between two boxes
python -m infra.dispatcher.dispatch config diff <aliasA> <aliasB>
```
