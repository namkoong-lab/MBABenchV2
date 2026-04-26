# infra — quick start

Operator guide for running gui-agents on EC2 boxes from your laptop. Two
reference docs worth keeping open:

- [dispatcher/common_commands.md](dispatcher/common_commands.md) — full `dispatch` CLI reference.
- [plan.md](plan.md) — architecture, config layering, lifecycle details.

## Layout

| Path | Purpose | Runs where |
|---|---|---|
| [run.py](run.py) | Executes one task end-to-end. | Box (called by worker); also usable on laptop |
| [configs/](configs/) | Layered config: defaults + your overrides + per-run profiles. | Both |
| [worker/](worker/) | Box-side state.json, queue CLI, worker loop, systemd units. | Each EC2 box |
| [dispatcher/](dispatcher/) | Laptop-side: `dispatch` CLI, box registry, `spinup.sh` / `teardown.sh`. | Laptop |

---

## First-time setup

### One-time prereqs (laptop)

1. **AWS CLI v2** installed and configured (`aws configure` or SSO).
2. **Fill in [configs/configs.yaml](configs/)** (gitignored) with at minimum
   `database.url`, `aws.access_key_id`, `aws.secret_access_key`, and the
   `agent.*` identity for the cohort you'll run. Schema:
   [configs/configs.default.yaml](configs/configs.default.yaml).
3. **AWS key pair + security group** — idempotent bootstrap:

   ```bash
   ./infra/dispatcher/aws_bootstrap.sh --region us-east-1
   ```

   Creates `bizbench-gui-agents` key (saved to `~/.ssh/bizbench-gui-agents.pem`)
   and `bizbench-gui-agents-sg` authorized from your current IP. Writes
   `dispatcher/.aws_defaults` so later scripts don't re-prompt.

### Per box

1. **Spin up** — launches, installs, registers in
   [dispatcher/boxes.yaml](dispatcher/):

   ```bash
   ./infra/dispatcher/spinup.sh --alias chatgpt-pro-1 \
     --config-template infra/dispatcher/config_templates/chatgpt_pro.yaml
   ```

   Templates in [dispatcher/config_templates/](dispatcher/config_templates/)
   pick provider/agent/model per box. Provider comes from the template's
   `provider.kind` — no separate flag. Takes ~2 min.

2. **Log in to the browser** — opens a VNC tunnel to the worker's Chrome:

   ```bash
   python -m infra.dispatcher.dispatch login chatgpt-pro-1
   ```

   Log in to claude.ai / chatgpt.com in the Chrome window. Cookies persist in
   Chrome's `--user-data-dir` and survive worker restarts. Re-do every few
   weeks when the session expires.

3. **Verify:**

   ```bash
   python -m infra.dispatcher.dispatch status
   ```

---

## Common workflows

```bash
# See who's doing what (add -f for live refresh)
python -m infra.dispatcher.dispatch status

# Backlog per cohort: in_flight / unassigned / remaining / total
python -m infra.dispatcher.dispatch backlog

# Pull 20 eligible tasks from the DB, split across matching boxes
python -m infra.dispatcher.dispatch assign --n 20

# Assign specific task ids, pin to one box
python -m infra.dispatcher.dispatch assign --tasks 42,43 --box claude-1

# Tail a running task's journal
python -m infra.dispatcher.dispatch logs claude-1 --task 42 -f

# Stop a task / drain a box's queue
python -m infra.dispatcher.dispatch cancel claude-1 42
python -m infra.dispatcher.dispatch clear claude-1

# Change a box's config — worker restarts automatically
python -m infra.dispatcher.dispatch config pull claude-1    # → /tmp/gui-agents-claude-1-configs.yaml
# edit the file, then:
python -m infra.dispatcher.dispatch config push claude-1 /tmp/gui-agents-claude-1-configs.yaml

# Re-login when a session expires (auth probe shows STALE in `status`)
python -m infra.dispatcher.dispatch login claude-1

# Refresh an `old <email>` login entry without going through VNC
python -m infra.dispatcher.dispatch probe claude-1
python -m infra.dispatcher.dispatch probe --all
```

`dispatch status` also detects `old` login entries after printing the
table and prompts you to kick a fresh auth probe on those boxes — useful
when the `checked_at` timestamp is stale but the cookie is still good.

## Tearing down

```bash
./infra/dispatcher/teardown.sh --alias claude-1    # one box
./infra/dispatcher/teardown.sh --all               # every gui-agents box
```

Terminates by the `Project=gui-agents` tag. The security group and key pair
are preserved for reuse. EBS is wiped, so the Chrome profile (and login
cookies) goes with it — you'll redo the browser login after a respin.

## Troubleshooting

| Symptom | Next step |
|---|---|
| `dispatch status` shows `UNREACHABLE` | Check your public IP vs. the SG (try `./infra/dispatcher/aws_bootstrap.sh` again to re-authorize). |
| `status` login column shows `STALE` | Run `dispatch login <alias>` and re-login. |
| `status` login column shows `old <email>` | Run `dispatch probe <alias>` (or just answer `y` at the status-table prompt) to re-verify the existing session. |
| Assigned a task, nothing starts | `dispatch logs <alias> -f`; on the box, `sudo systemctl status gui-agents-worker`. |
| Task fails with "Chrome not reachable on CDP port …" | On the box: `sudo systemctl status gui-agents-chrome`; restart if dead. |
| `config push` rejected | The YAML didn't parse. `config pull` and diff against your edit. |
