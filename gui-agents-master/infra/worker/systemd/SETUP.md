# Box setup — EC2 worker

> **`spinup.sh` automates all of this.** Prefer
> `./infra/dispatcher/spinup.sh --alias <name> --config-template <path>`
> and re-run it against an existing alias to push code/config updates.
> This doc is the manual fallback — useful for diagnosis or when rebuilding
> a box from scratch without the dispatcher.

One-time install per box. Run as root (or wrap with `sudo`).

## 1. Install the repo

```bash
sudo git clone <your-fork-url> /opt/gui-agents-master
cd /opt/gui-agents-master
sudo pip3 install -r requirements.txt  # or a pinned subset: boto3 psycopg2-binary pyyaml
```

## 2. Drop the queue CLI wrapper onto PATH

```bash
sudo install -m 0755 /opt/gui-agents-master/infra/worker/systemd/gui-agents-queue \
  /usr/local/bin/gui-agents-queue
```

Verify:
```bash
gui-agents-queue show
```

## 3. Create state dir and secrets file

```bash
sudo mkdir -p /var/lib/gui-agents
sudo mkdir -p /etc/gui-agents
sudo tee /etc/gui-agents/secrets.env >/dev/null <<'EOF'
MBABENCHV2JUDGE_KEYS_DATABASE_URL=postgres://...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
EOF
sudo chmod 0600 /etc/gui-agents/secrets.env
```

## 4. Seed configs.yaml

Copy a project-wide config for this box. From the laptop:
```bash
python -m infra.dispatcher.dispatch config push <alias> ./path/to/configs.yaml
```
(Or `scp` it into place manually the first time, then SSH in to set perms.)

## 5. Install the worker systemd unit

```bash
sudo install -m 0644 /opt/gui-agents-master/infra/worker/systemd/gui-agents-worker.service \
  /etc/systemd/system/gui-agents-worker.service

# If using EnvironmentFile, uncomment the line in the unit:
sudo sed -i 's|^# EnvironmentFile=|EnvironmentFile=|' \
  /etc/systemd/system/gui-agents-worker.service

sudo systemctl daemon-reload
sudo systemctl enable --now gui-agents-worker.service
sudo systemctl status gui-agents-worker.service
```

## 6. Verify from the laptop

`spinup.sh` has already registered the box in [../../dispatcher/boxes.yaml](../../dispatcher/boxes.yaml) (if you used it). Verify:
```bash
python -m infra.dispatcher.dispatch status
python -m infra.dispatcher.dispatch assign --tasks <known-good-task-id> --box <alias>
python -m infra.dispatcher.dispatch logs <alias> --task <id> -f
```

## Sudoers note

`dispatch cancel` (`systemctl stop gui-agents-task-<id>`) and
`gui-agents-queue config push` (worker restart) run as the SSH user and call
`sudo -n systemctl ...`; without passwordless sudo systemd answers with
"Interactive authentication required". The AWS Ubuntu AMI gives `ubuntu` full
NOPASSWD sudo, so nothing extra is needed there. On a box where that was
tightened, add a rule for the SSH user:

```
ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart gui-agents-worker.service, \
                            /bin/systemctl stop gui-agents-task-*, \
                            /bin/systemctl start gui-agents-chrome.service, \
                            /bin/systemctl start gui-agents-auth-probe.service
```

(`systemctl is-active` needs no privileges, so `worker_loop.py` calls it
directly.) Simpler alternative: run the worker as root and SSH in as root.
