"""Box registry — reads infra/dispatcher/boxes.yaml.

Schema:

    boxes:
      - alias: claude-1               # required; how dispatch refers to it
        instance_id: i-0abc123         # optional; informational
        ssh_host: ec2-xxx.amazonaws.com  # required
        ssh_user: ubuntu               # optional; defaults to 'ubuntu'
        ssh_key: ~/.ssh/bizbench-gui-agents.pem # optional; ssh default resolution if omitted
        ssh_port: 22                   # optional; defaults to 22

The file is gitignored (like configs.yaml) because entries are account- and
machine-specific. spinup.sh auto-appends here after launching a box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REGISTRY_PATH = Path(__file__).resolve().parent / "boxes.yaml"


@dataclass(frozen=True)
class Box:
    alias: str
    ssh_host: str
    ssh_user: str = "ubuntu"
    ssh_key: str | None = None
    ssh_port: int = 22
    instance_id: str | None = None

    def ssh_target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}"

    def ssh_base_args(self) -> list[str]:
        args = [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-p", str(self.ssh_port),
        ]
        if self.ssh_key:
            args += ["-i", os.path.expanduser(self.ssh_key)]
        return args


def _load_raw() -> dict:
    if not REGISTRY_PATH.exists():
        raise FileNotFoundError(
            f"box registry not found at {REGISTRY_PATH}. "
            f"Run `./infra/dispatcher/spinup.sh` to launch + auto-register a "
            f"box, or create the file manually — see the schema in "
            f"infra/dispatcher/boxes.py's module docstring."
        )
    data = yaml.safe_load(REGISTRY_PATH.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{REGISTRY_PATH}: top level must be a mapping")
    return data


def load_boxes() -> list[Box]:
    data = _load_raw()
    raw_boxes = data.get("boxes") or []
    if not isinstance(raw_boxes, list):
        raise ValueError(f"{REGISTRY_PATH}: 'boxes' must be a list")
    out: list[Box] = []
    seen_aliases: set[str] = set()
    for i, entry in enumerate(raw_boxes):
        if not isinstance(entry, dict):
            raise ValueError(f"{REGISTRY_PATH}: boxes[{i}] must be a mapping")
        alias = entry.get("alias")
        ssh_host = entry.get("ssh_host")
        if not alias or not ssh_host:
            raise ValueError(
                f"{REGISTRY_PATH}: boxes[{i}] is missing required "
                f"'alias' or 'ssh_host'"
            )
        if alias in seen_aliases:
            raise ValueError(f"{REGISTRY_PATH}: duplicate alias {alias!r}")
        seen_aliases.add(alias)
        out.append(
            Box(
                alias=alias,
                ssh_host=ssh_host,
                ssh_user=entry.get("ssh_user", "ubuntu"),
                ssh_key=entry.get("ssh_key"),
                ssh_port=int(entry.get("ssh_port", 22)),
                instance_id=entry.get("instance_id"),
            )
        )
    return out


def find_by_alias(alias: str) -> Box:
    boxes = load_boxes()
    for b in boxes:
        if b.alias == alias:
            return b
    known = ", ".join(b.alias for b in boxes) or "(none)"
    raise KeyError(f"unknown box alias {alias!r}. Known: {known}")
