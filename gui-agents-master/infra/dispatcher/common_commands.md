# Dispatcher — common commands

All commands run from the repo root as `python -m infra.dispatcher.dispatch <...>`.
`<alias>` refers to a box alias defined in [boxes.yaml](boxes.yaml).

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

## Config (box-local configs.yaml)

```bash
# Pull remote config to a temp file; prints the path
python -m infra.dispatcher.dispatch config pull <alias>

# Push a local file as the new remote config
python -m infra.dispatcher.dispatch config push <alias> <localfile>

# Diff the live config between two boxes
python -m infra.dispatcher.dispatch config diff <aliasA> <aliasB>
```
