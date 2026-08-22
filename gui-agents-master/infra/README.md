# infra — quick start

> **Audience: MBABenchV2 internal team.** This guide assumes access to our private Postgres database, our `mbabenchv2` S3 bucket, and our internal AWS account. **External users:** the local quickstart in [`../README.md`](../README.md) is the supported turnkey path. The dispatcher code below is reusable against your own AWS / Postgres / S3, but you'd need to provision those yourself — see "BYO infrastructure" in the main README.

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
| [dispatcher/](dispatcher/) | Laptop-side: `dispatch` CLI (box lifecycle + task dispatch), box registry. | Laptop |

---

## First-time setup

### One-time prereqs (laptop)

1. **AWS CLI v2** installed and configured (`aws configure` or SSO).
2. **Fill in `<repo>/config/config.yaml`** (gitignored, one level above
   `gui-agents-master/`) with `database.v1_url`, `database.v2_url`,
   `aws.access_key_id`, and `aws.secret_access_key`. Both database urls live
   there together and the run config's `benchmark:` key picks between them,
   so there is nothing to swap by hand when moving between experiments.

   Credentials no longer live in `infra/configs/configs.yaml` — that file is
   now purely the box-side run profile. See the `database:` / `aws:` comment
   blocks in [configs/configs.default.yaml](configs/configs.default.yaml) for
   the full resolution order.
3. **AWS key pair + security group** — idempotent bootstrap:

   ```bash
   dispatch bootstrap --region us-east-1
   ```

   Prompts for a key-pair and security-group name on first run, creates both,
   and writes your answers to `aws.gui_key_name` / `aws.gui_sg_name` in
   `<repo>/config/config.yaml` — so it only ever asks once, and everything
   afterwards reads the names from there. The private key is saved to
   `~/.ssh/<aws.gui_key_name>.pem`; the group allows SSH from your current IP.
   Also writes `dispatcher/.aws_defaults` (region + the account id, used to
   refuse work against the wrong AWS account).

### Per box

1. **Spin up** — launches, installs, registers in
   [dispatcher/boxes.yaml](dispatcher/):

   ```bash
   dispatch spinup --alias chatgpt-pro-1 \
     --config-template infra/dispatcher/config_templates/chatgpt_pro.yaml
   ```

   Templates in [dispatcher/config_templates/](dispatcher/config_templates/)
   pick provider/agent/model per box. Provider (`provider.kind`) and the
   database (`benchmark`, selecting `database.v1_url` or `database.v2_url`)
   both come from the template — no separate flags, so a box cannot be
   pointed at one experiment's tasks and the other's database. Takes ~2 min.

   Key pair and security group come from `aws.gui_key_name` / `aws.gui_sg_name`
   in `<repo>/config/config.yaml`, which is also where the AWS credentials
   live — so the fleet is always created in the same account that owns the S3
   bucket the boxes write to. There is no `--key-name` / `--sg-id`.

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

## Pausing vs tearing down

Between runs you almost always want **stop**, not teardown:

```bash
python -m infra.dispatcher.dispatch stop claude-1    # one box
python -m infra.dispatcher.dispatch stop --all       # every registered box
```

Stopping halts the instance — compute billing ends — but keeps the EBS root
volume, so the Chrome profile and its logged-in cookies are still there next
time. Restart with the ordinary spinup command; on a stopped box it starts the
instance again instead of launching a new one, and re-pushes the current code
and config:

```bash
python -m infra.dispatcher.dispatch spinup --alias claude-1 \
  --config-template infra/dispatcher/config_templates/claude_fable5_chat.yaml
```

A stopped box gets a **new public DNS** when it starts, so `boxes.yaml` is
rewritten on both stop and start. Don't cache the hostname anywhere else.

`stop` refuses while a task is in flight — stopping mid-task loses the attempt
entirely, since it is neither recorded in `task_attempts` nor requeued. Pass
`--force` if you mean it.

```bash
python -m infra.dispatcher.dispatch teardown claude-1   # one box
python -m infra.dispatcher.dispatch teardown --all      # every gui-agents box
```

Teardown terminates, selecting by the `Project=gui-agents` tag. The security
group and key pair are preserved for reuse, but EBS is wiped — the Chrome
profile and login cookies go with it, so you redo the browser login after a
respin. Use it when you want the storage cost to stop too; a stopped box's
root volume still bills (~30 GiB gp3).

## Troubleshooting

| Symptom | Next step |
|---|---|
| `dispatch status` shows `UNREACHABLE` | Check your public IP vs. the SG (try `dispatch bootstrap` again to re-authorize). |
| `status` login column shows `STALE` | Run `dispatch login <alias>` and re-login. |
| `status` login column shows `old <email>` | Run `dispatch probe <alias>` (or just answer `y` at the status-table prompt) to re-verify the existing session. |
| Assigned a task, nothing starts | `dispatch logs <alias> -f`; on the box, `sudo systemctl status gui-agents-worker`. |
| Task fails with "Chrome not reachable on CDP port …" | On the box: `sudo systemctl status gui-agents-chrome`; restart if dead. |
| `config push` rejected | The YAML didn't parse. `config pull` and diff against your edit. |
