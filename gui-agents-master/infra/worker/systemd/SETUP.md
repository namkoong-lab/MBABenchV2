# Box setup — EC2 worker

> **`dispatch spinup` automates all of this except the `house_standards/` copy in step 1** (it rsyncs only `gui-agents-master/`; boxes running prompt 204/205 also need `/opt/house_standards/`). Prefer
> `python -m infra.dispatcher.dispatch spinup --alias <name> --config-template <path>`
> (from `gui-agents-master/`) and re-run it against an existing alias to push code/config updates.
> This doc is the manual fallback — useful for diagnosis or when rebuilding
> a box from scratch without the dispatcher.

One-time install per box. Run as root (or wrap with `sudo`).

## 1. Install the repo

```bash
# The units expect gui-agents-master/ itself at /opt/gui-agents-master (what `dispatch spinup` rsyncs);
# prompt versions 204/205 also attach ../house_standards/House_Standards_v1.md
git clone <your-fork-url> /tmp/spreadsheetsmith
sudo cp -r /tmp/spreadsheetsmith/gui-agents-master /opt/gui-agents-master
sudo cp -r /tmp/spreadsheetsmith/house_standards /opt/house_standards
cd /opt/gui-agents-master
# [project].dependencies of pyproject.toml; the units reach the code through PYTHONPATH
sudo pip3 install playwright pyyaml python-dotenv openpyxl boto3 psycopg2-binary
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
SPREADSHEETSMITHJUDGE_KEYS_DATABASE_URL=postgres://...
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

## 5. Install the systemd units

The worker unit requires `xvfb.service` and only connects to the Chrome that
`gui-agents-chrome.service` owns, so install all five (what `dispatch spinup` installs):

```bash
for unit in xvfb.service gui-agents-chrome.service gui-agents-worker.service \
            gui-agents-auth-probe.service gui-agents-auth-probe.timer; do
  sudo install -m 0644 /opt/gui-agents-master/infra/worker/systemd/$unit \
    /etc/systemd/system/$unit
done

# Uncomment the (indented) EnvironmentFile line in the worker unit:
sudo sed -i 's|^#[[:space:]]*EnvironmentFile=|EnvironmentFile=|' \
  /etc/systemd/system/gui-agents-worker.service

sudo systemctl daemon-reload
sudo systemctl enable --now xvfb.service gui-agents-chrome.service \
  gui-agents-worker.service gui-agents-auth-probe.timer
sudo systemctl status gui-agents-worker.service
```

## 6. Verify from the laptop

`dispatch spinup` registers the box in `infra/dispatcher/boxes.yaml` (gitignored); after a manual install add the entry yourself (schema in [`../../dispatcher/helper/boxes.py`](../../dispatcher/helper/boxes.py)). Verify:
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
