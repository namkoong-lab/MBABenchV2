# infra — operator guide

Day-to-day guide for running gui-agents across one or more EC2 boxes from your laptop. For the architectural design, see [plan.md](plan.md).

## What's in here

| Path | Purpose | Runs where |
|---|---|---|
| [run.py](run.py) | Executes one batch of tasks end-to-end. | Laptop (ad-hoc) or box (called by worker) |
| [configs/](configs/) | Layered config (defaults, user overrides, run-configs). | Both |
| [worker/](worker/) | Box-side: `state.json`, queue CLI, worker loop, systemd units. | Each EC2 box |
| [dispatcher/](dispatcher/) | Laptop-side: `dispatch` CLI, box registry, AWS `spinup.sh` / `teardown.sh`. | Laptop |

---

## Setup

Bringing up a usable box is four steps. Do step 1 once; do steps 2–4 each time you add a new box.

### 1. Laptop prerequisites (one-time)

1. **AWS CLI v2** installed and configured (`aws configure` or SSO).

2. **AWS key pair + security group.** Run the bootstrap helper once — it creates both if missing and authorizes SSH from your current IP:

   ```bash
   ./infra/dispatcher/aws_bootstrap.sh --region us-east-1
   ```

   Defaults: key `bizbench-gui-agents` (saved to `~/.ssh/bizbench-gui-agents.pem`) and security group `bizbench-gui-agents-sg`. It's idempotent — safe to re-run if your IP changes (pass `--prune-ips` to clear old CIDRs first). Prints the `--key-name` and `--sg-id` values you pass to `spinup.sh` next.

3. **DB + AWS credentials** filled into [configs/configs.yaml](configs/configs.yaml) (gitignored). `spinup.sh` reads `database.url`, `aws.access_key_id`, and `aws.secret_access_key` from this file and writes them into `/etc/gui-agents/secrets.env` on the box, so without these values spinup refuses to launch. See [configs/configs.default.yaml](configs/configs.default.yaml) for the full schema.

4. **Box registry** at [dispatcher/boxes.yaml](dispatcher/boxes.yaml) (gitignored). `spinup.sh` creates and appends to it automatically — you won't normally edit it by hand.

### 2. Spin up the EC2 box

```bash
./infra/dispatcher/spinup.sh --alias chatgpt-pro-1 \
  --config-template infra/dispatcher/config_templates/chatgpt_pro.yaml
```

`--config-template` points at the sparse-overlay `configs.yaml` the box should use (picks provider, agent, model, task source, etc.). Ready-made templates live in [dispatcher/config_templates/](dispatcher/config_templates/); add one per box variant. Provider is read from the template's `provider.kind` — no separate flag.

`aws_bootstrap.sh` wrote `infra/dispatcher/.aws_defaults` with your key-name / sg-id / region — `spinup.sh` sources it automatically. Override any of them with `--key-name`, `--sg-id`, `--region`, or `AWS_REGION=...` when you want a different choice.

The script:

- Launches a `t3.medium` Ubuntu 22.04 box (override via `--instance-type`).
- Tags it `Project=gui-agents`, `alias=<name>`, `provider=<claude|chatgpt>` — `teardown.sh` finds it by these.
- Attaches cloud-init that installs Python, git, Xvfb, Google Chrome, x11vnc, tmux while you wait (~2 min). Readiness marker: `/var/lib/gui-agents/.bootstrap-done`.
- **Auto-appends the box to [dispatcher/boxes.yaml](dispatcher/boxes.yaml)**, so `dispatch status` sees it immediately.
- rsyncs the local repo to `/opt/gui-agents-master`, `pip install`s requirements, installs `gui-agents-queue` + `gui-agents-worker.service`, synthesizes `/etc/gui-agents/secrets.env` from your laptop's configs.yaml, copies the template into place, and `enable --now`s the worker. No manual SSH step needed — the box is ready when the script prints its summary.

**Re-run semantics.** Calling `spinup.sh --alias <existing>` again re-rsyncs the repo, re-pushes secrets + configs, and restarts the worker. Refuses unless the worker is **strictly idle** (no current task, empty queue) — inspect with `dispatch show <alias>` first. Useful for picking up local code changes without tearing the box down. For config-only changes on a running box, prefer `dispatch config push` (lighter-weight; see below).

The box-side install steps are fully documented in [worker/systemd/SETUP.md](worker/systemd/SETUP.md) — that's now a fallback/diagnosis reference; you shouldn't need to run those commands by hand.

### 3. Log in to Claude.ai / ChatGPT.com (per box, first time only)

The engine drives a real browser session — cookies must exist in Chrome's `--user-data-dir`. Do this once per box:

```bash
# on your laptop — open an SSH tunnel for VNC
ssh -i ~/.ssh/bizbench-gui-agents.pem -L 5901:localhost:5901 ubuntu@<public_dns>

# on the box — start VNC against the existing Xvfb display
x11vnc -display :99 -localhost -nopw -rfbport 5901 &

# on your laptop — connect a VNC viewer to localhost:5901, log in to
# claude.ai / chatgpt.com in the Chrome window you see, then exit.
```

Cookies persist under `/var/lib/gui-agents/chrome-claude` (or `chrome-chatgpt`). Repeat every few weeks when sessions expire.

A systemd timer (`gui-agents-auth-probe.timer`) runs every 5 minutes and fills a `login` column in `dispatch status` — `ok`, `STALE` (re-login needed), `old` (last success is > 30 min old), or `?` (no result yet). To trigger a probe on demand: `sudo systemctl start gui-agents-auth-probe.service` on the box.

### 4. Verify the box is registered

`spinup.sh` already appended the new box to [dispatcher/boxes.yaml](dispatcher/boxes.yaml). Inspect it:

```yaml
boxes:
  - alias: claude-1
    instance_id: i-0abc123
    ssh_host: ec2-xxx.compute.amazonaws.com
    ssh_user: ubuntu
    ssh_key: ~/.ssh/bizbench-gui-agents.pem
```

Sanity check:

```bash
python -m infra.dispatcher.dispatch status
```

You should see `claude-1` with its agent summary.

### Tearing down

```bash
./infra/dispatcher/teardown.sh --alias claude-1            # one box
./infra/dispatcher/teardown.sh --all                       # every gui-agents box
```

Finds instances by `Project=gui-agents` tag, prints the list, asks for confirmation, then terminates. The security group and key pair are **not** deleted — they're reused on respin. Terminating wipes the EBS root volume, so the Claude/ChatGPT login cookies are lost; you'll redo step 3 on the next spin-up.

### Reset from scratch (testing the setup flow itself)

Use [_reset_dispatcher_status.sh](dispatcher/_reset_dispatcher_status.sh) when you want a clean slate — for example, to re-test the `aws_bootstrap → spinup` path end-to-end.

```bash
./infra/dispatcher/_reset_dispatcher_status.sh -y
./infra/dispatcher/aws_bootstrap.sh
./infra/dispatcher/spinup.sh --alias chatgpt-pro-1 \
  --config-template infra/dispatcher/config_templates/chatgpt_pro.yaml
```

The reset script is **destructive** (underscore prefix is a convention so it doesn't tab-complete alongside the others). It terminates every `Project=gui-agents` EC2 instance, deletes the AWS key pair + security group, removes `~/.ssh/bizbench-gui-agents.pem`, and wipes the two gitignored local state files. `aws_bootstrap.sh` then recreates everything from zero.

### Gitignored local state

Three files accumulate locally and are in [.gitignore](../.gitignore):

| File | Writer | Holds |
|---|---|---|
| `infra/configs/configs.yaml` | you (once), then `dispatch config push` | DB url, AWS creds, agent identity. |
| `infra/dispatcher/.aws_defaults` | `aws_bootstrap.sh` | Shell-sourceable `GUI_AGENTS_*` defaults — key name, SG id, region. Read by `spinup.sh` / `teardown.sh` / reset. |
| `infra/dispatcher/boxes.yaml` | `spinup.sh` (auto-append) | The box registry `dispatch.py` reads. |

`_reset_dispatcher_status.sh` wipes all three in one shot.

---

## Worker — what runs on each box

`gui-agents-worker.service` is a long-running Linux service. Its loop:

1. Every 5s, check `state.json` for a queued task.
2. If idle and the queue is non-empty: pop the head, launch `python -m infra.run --task-id <id>` inside a transient `systemd-run --unit=gui-agents-task-<id>` unit.
3. When that unit exits, mark the task `success` / `failed` in `state.json`; the sink has already written the final row to the DB.

Tasks run **only** when something enqueues them — either `dispatch assign` from the laptop or a manual `gui-agents-queue add` on the box.

**Useful commands on the box (SSH in):**

```bash
sudo systemctl status gui-agents-worker         # is it up?
sudo journalctl -u gui-agents-worker -f         # live logs
sudo systemctl restart gui-agents-worker        # reload config
gui-agents-queue show                           # what's queued / running
gui-agents-queue add <task_id> "<task_name>"    # manual enqueue (dispatch uses this too)
```

---

## Dispatch — laptop control plane

Everything goes through `dispatch`, which fans out over SSH.

```bash
python -m infra.dispatcher.dispatch <subcommand> [args]
```

### Commands

| Command | What it does |
|---|---|
| `status` | One-line summary per box: agent, current task, queue depth, last completed. |
| `status -f` | Live refresh every 5s. |
| `show <alias>` | Full `state.json` for one box. |
| `assign --n 20 [--agent X] [--task-source Y]` | Pick N un-attempted tasks from DB; distribute to matching boxes by least-loaded. |
| `assign --tasks 42,43 [--box <alias>]` | Assign specific task IDs; pin to one box if `--box` given. |
| `cancel <alias> <task_id>` | Stop it if running; remove from queue if queued. |
| `clear <alias>` | Drop all queued tasks. The currently running task is *not* cancelled. |
| `logs <alias> [--task <id>] [-f]` | Tail the worker service, or one task's journal. |
| `config pull <alias>` | Download the box's `configs.yaml` to `/tmp/gui-agents-<alias>-configs.yaml`. |
| `config push <alias> <file>` | Upload a new `configs.yaml` and restart the worker. |
| `config diff <a> <b>` | Unified diff of two boxes' configs (drift check). |

---

## Common workflows

### Run a batch overnight

```bash
python -m infra.dispatcher.dispatch status                        # all boxes up?
python -m infra.dispatcher.dispatch assign --n 50                 # queue 50 tasks
# go home
python -m infra.dispatcher.dispatch status                        # check in morning
```

### Run a single task for debugging

```bash
python -m infra.dispatcher.dispatch assign --tasks 142 --box claude-1
python -m infra.dispatcher.dispatch logs claude-1 --task 142 -f
```

### Change a box's settings

```bash
python -m infra.dispatcher.dispatch config pull claude-1
# edit the file it prints (e.g. /tmp/gui-agents-claude-1-configs.yaml)
python -m infra.dispatcher.dispatch config push claude-1 /tmp/gui-agents-claude-1-configs.yaml
# worker auto-restarts; first task afterward uses the new config
```

### Check for config drift across boxes

```bash
python -m infra.dispatcher.dispatch config diff claude-1 claude-2
```

### Stop a task that's going nowhere

```bash
python -m infra.dispatcher.dispatch cancel claude-1 142
```

### Drain a box before a reboot

```bash
python -m infra.dispatcher.dispatch clear claude-1         # stop accepting new work
# wait for `status` to show current=idle
# then reboot / restart worker / etc.
```

---

## Troubleshooting

| Symptom | Likely cause | Next step |
|---|---|---|
| `dispatch status` shows `UNREACHABLE` | Box down / wrong SSH creds in `boxes.yaml`. | SSH directly to the box using the `ssh_host` from `boxes.yaml`. |
| Assigned a task, nothing starts | Worker service is dead. | `ssh <host> sudo systemctl status gui-agents-worker`. Restart if needed. |
| `dispatch assign` finds 0 eligible tasks | `skip_already_attempted` filtering everything. | Query the DB directly, or drop the filter in the laptop's run-config. |
| Task keeps failing | Check the S3-uploaded completion log attached to the failed `task_attempts` row. | Follow the error; usually Claude/ChatGPT session expired (re-VNC to re-login). |
| Worker running but stuck on one task | Chrome hung or network issue. | `dispatch cancel <alias> <task_id>` then investigate logs. |
| Config push rejected | New `configs.yaml` doesn't parse as YAML. | `config pull` the current one, diff against your edit. |
