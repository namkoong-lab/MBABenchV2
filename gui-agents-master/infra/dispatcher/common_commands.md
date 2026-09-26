# Dispatcher — common commands

All commands run from the repo root as `python -m infra.dispatcher.dispatch <...>`.
`<alias>` refers to a box alias defined in [boxes.yaml](boxes.yaml).

`status` / `show` / `assign` check whether your current public IP is in the
dispatcher security group before fanning out over SSH. If it isn't, they print
a warning pointing at `dispatch bootstrap` — re-run that (or set
`DISPATCH_NO_DIAGNOSE=1` to suppress the check).

## Spin up / tear down

`spinup` prompts for the instance type unless `--instance-type` is given
(`-y` or a non-TTY takes `t3.large`). **t3.medium OOMs on ChatGPT-in-Chrome
runs** — observed 2026-08-22, when Chrome hit 3.2 GiB RSS on the 4 GiB box,
the OOM killer fired and the host swap-thrashed until sshd stopped answering.
Use `t3.large` for ChatGPT boxes.

On an alias that is already in `boxes.yaml`, choosing a different type resizes
the instance in place — it is stopped, retyped and started again, so the root
volume and the browser login on it survive. The public DNS changes, and
`boxes.yaml` is updated with it.

```bash
# Launch (or re-provision) a box from a config template
dispatch spinup --alias chatgpt-sol56-chat-1 --config-template infra/dispatcher/config_templates/chatgpt_sol56_chat.yaml

# Skip the prompt
dispatch spinup --alias chatgpt-sol56-chat-1 --config-template ... --instance-type t3.large

# Terminate one box by alias
dispatch teardown --alias chatgpt-pro-1

# Terminate every gui-agents box in the region
dispatch teardown --all
```

## Rename a box

Changes the alias in [boxes.yaml](boxes.yaml) **and** the instance's `alias` /
`Name` EC2 tags, which is why it's a command rather than an edit: `teardown
--all` and spinup's recovery path filter on `tag:alias`, so a hand-edited
registry leaves the box unfindable under the name you now use.

The alias is laptop-side only — nothing on the box records it — so renaming is
safe while a task is in flight.

```bash
python -m infra.dispatcher.dispatch rename <old_alias> <new_alias>

# Registry only, no AWS calls (leaves the tags stale — for a terminated box,
# or when the config.yaml credentials aren't to hand)
python -m infra.dispatcher.dispatch rename <old> <new> --skip-tags
```

Tags are written before the registry: if the AWS call fails, nothing has
changed and the box still answers to its old name. It refuses a name that is
already registered, and restricts the new alias to letters, digits, `.`, `_`
and `-` — the registry is line-based YAML, and other characters would need
quoting to survive the round-trip.

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
# Forwards box:5901 -> the first free local port of 5901, 5911, 5921, …
# Run it again for a second box and it picks the next one automatically.
python -m infra.dispatcher.dispatch login <alias>

# Pin the local port instead of letting it scan
python -m infra.dispatcher.dispatch login <alias> --local-port 5911

# Don't auto-launch the VNC viewer
python -m infra.dispatcher.dispatch login <alias> --no-open
```

The chosen port is printed (`local port 5901 busy — using 5911`) and appears
in the `vnc://localhost:<port>` line. A pinned `--local-port` that is already
taken is a hard error rather than a tunnel-less session, so the viewer can
never open onto another box's forward.

In the VNC session: log in through the already-running Chrome window, then
Ctrl-C the dispatcher terminal to tear down the tunnel.

`login` refuses to run while a task is in flight — it opens a page in the
worker's Chrome, which would collide with the agent. To just *look* at a
running box, use `watch`.

## Watch a running box (read-only VNC)

Same VNC tunnel, no interference: `watch` never connects to Chrome over CDP,
never opens a page, never kicks the auth probe, and runs x11vnc `-viewonly`,
so clicks and keystrokes in your viewer are dropped instead of landing in the
agent's browser. Safe to attach and detach mid-task.

It binds its own port on the box (5902 vs `login`'s 5901), so watching does
not evict an in-flight login session and vice versa.

```bash
# Forwards box:5902 -> the first free local port of 5902, 5912, 5922, …
python -m infra.dispatcher.dispatch watch <alias>

# Pin the local port / no auto-viewer
python -m infra.dispatcher.dispatch watch <alias> --local-port 5912 --no-open
```

Ctrl-C tears down the tunnel and the x11vnc it started; the task keeps running.

### Watching several boxes at once

Just run `watch` in a second terminal — the local port scans 5902, 5912, 5922, …
and takes the first free one, so concurrent sessions no longer collide. The
`login` family scans 5901, 5911, 5921, …, which is why the stride is 10: the two
never land on the same port.

Each viewer is a separate `host:port`, so macOS opens a genuinely separate
Screen Sharing window per box. Two things to keep straight:

- **Every run mints a fresh one-shot VNC password.** Read each from its own
  terminal — reusing the previous one looks exactly like a rejected password.
- **A stale window blocks a new one.** `open vnc://localhost:<port>` re-activates
  an existing Screen Sharing window for that same `host:port` instead of
  connecting, and the password it holds is already dead. If no window appears,
  quit Screen Sharing (`osascript -e 'quit app "Screen Sharing"'`) and re-run.

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
