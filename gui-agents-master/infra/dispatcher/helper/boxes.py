"""Box registry — reads infra/dispatcher/boxes.yaml.

Schema:

    boxes:
      - alias: claude-1               # required; how dispatch refers to it
        instance_id: i-0abc123         # optional; informational
        ssh_host: ec2-xxx.amazonaws.com  # required while running
        ssh_user: ubuntu               # optional; defaults to 'ubuntu'
        ssh_key: ~/.ssh/<aws.gui_key_name>.pem # optional; ssh default resolution if omitted
        ssh_port: 22                   # optional; defaults to 22
        instance_type: t3.large        # optional; informational, last observed
        status: running                # optional; running | stopped

The file is gitignored (like configs.yaml) because entries are account- and
machine-specific. `dispatch spinup` appends here after launching a box.

`status` is a CACHE, never an authority. EC2 is the authority: anyone can stop
a box from the console without telling this file. The provisioning commands
read the real state from AWS and write it back here, so the field reflects
what was last observed rather than driving any decision.

`ssh_host` needs the same suspicion. A stopped instance has no public DNS, and
gets a NEW one when it starts again (there is no Elastic IP) — so a host
cached from before a stop points at nothing, or eventually at someone else's
instance. `dispatch stop` blanks it; `dispatch spinup` refreshes it on start.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# parents[1], not parent: this module lives in dispatcher/helper/, but the
# registry is operator state and stays in dispatcher/ alongside .aws_defaults.
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "boxes.yaml"

STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"


@dataclass(frozen=True)
class Box:
    alias: str
    ssh_host: str
    ssh_user: str = "ubuntu"
    ssh_key: str | None = None
    ssh_port: int = 22
    instance_id: str | None = None
    instance_type: str | None = None
    status: str | None = None

    @property
    def is_stopped(self) -> bool:
        """Last observed as stopped, or has no host to connect to.

        A blank ssh_host means the same thing in practice — `dispatch stop`
        clears it precisely because a stopped instance has no address.
        """
        return self.status == STATUS_STOPPED or not self.ssh_host

    def ssh_target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}"

    def ssh_base_args(self) -> list[str]:
        args = [
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
            "-p",
            str(self.ssh_port),
        ]
        if self.ssh_key:
            args += ["-i", os.path.expanduser(self.ssh_key)]
        return args


def _load_raw() -> dict:
    if not REGISTRY_PATH.exists():
        raise FileNotFoundError(
            f"box registry not found at {REGISTRY_PATH}. "
            f"Run `dispatch spinup` to launch + auto-register a "
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
        if not alias:
            raise ValueError(f"{REGISTRY_PATH}: boxes[{i}] is missing 'alias'")
        # ssh_host is allowed to be empty: a stopped instance has no public
        # DNS, and `dispatch stop` blanks it rather than leaving an address
        # that no longer routes here. Commands that need to connect check
        # Box.is_stopped and say so.
        if alias in seen_aliases:
            raise ValueError(f"{REGISTRY_PATH}: duplicate alias {alias!r}")
        seen_aliases.add(alias)
        out.append(
            Box(
                alias=alias,
                ssh_host=entry.get("ssh_host") or "",
                ssh_user=entry.get("ssh_user", "ubuntu"),
                ssh_key=entry.get("ssh_key"),
                ssh_port=int(entry.get("ssh_port", 22)),
                instance_id=entry.get("instance_id"),
                instance_type=entry.get("instance_type"),
                status=entry.get("status"),
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


# ---------------------------------------------------------------------------
# Registry writes
#
# Line-based rather than a YAML round-trip. safe_dump would rewrite quoting,
# ordering and any hand-added comments across the whole file just to change
# one scalar, and this registry is something operators edit by hand.
# ---------------------------------------------------------------------------

_ENTRY_RE = re.compile(r"^(\s*)-\s*alias:\s*(.*?)\s*$")


def _entry_span(lines: list[str], alias: str) -> tuple[int, int, str]:
    """(start, end, field_indent) for one alias's block. Raises if absent."""
    start = indent = None
    for i, line in enumerate(lines):
        m = _ENTRY_RE.match(line)
        if m is None:
            continue
        if start is not None:  # the next entry closes the one we matched
            return start, i, indent  # type: ignore[return-value]
        if m.group(2) == alias:
            start = i
            # Fields sit one level in from the "- alias:" marker; the dash and
            # the space after it are what that level is made of.
            indent = m.group(1) + "  "
    if start is None:
        raise KeyError(f"alias {alias!r} not found in {REGISTRY_PATH}")
    return start, len(lines), indent  # type: ignore[return-value]


def set_field(alias: str, key: str, value: str) -> None:
    """Set (or insert) one field on one box's entry."""
    lines = REGISTRY_PATH.read_text().splitlines(keepends=True)
    start, end, indent = _entry_span(lines, alias)
    new = f"{indent}{key}: {value}\n" if value != "" else f"{indent}{key}:\n"
    field = re.compile(rf"^\s*{re.escape(key)}\s*:")
    for i in range(start + 1, end):
        if field.match(lines[i]):
            lines[i] = new
            break
    else:
        last = max((i for i in range(start, end) if lines[i].strip()), default=start)
        lines.insert(last + 1, new)
    REGISTRY_PATH.write_text("".join(lines))


def rename_box(old: str, new: str) -> None:
    """Change one entry's alias, leaving every other line byte-identical."""
    lines = REGISTRY_PATH.read_text().splitlines(keepends=True)
    start, _, _ = _entry_span(lines, old)
    indent = _ENTRY_RE.match(lines[start]).group(1)  # type: ignore[union-attr]
    lines[start] = f"{indent}- alias: {new}\n"
    REGISTRY_PATH.write_text("".join(lines))


def add_box(
    *,
    alias: str,
    instance_id: str,
    ssh_host: str,
    ssh_key: str,
    instance_type: str | None = None,
    status: str = STATUS_RUNNING,
    ssh_user: str = "ubuntu",
) -> None:
    """Append a new box entry, creating the registry if needed."""
    if not REGISTRY_PATH.exists() or not REGISTRY_PATH.read_text().strip():
        REGISTRY_PATH.write_text("boxes:\n")
    text = REGISTRY_PATH.read_text()
    if not text.endswith("\n"):
        text += "\n"
    text += (
        f"  - alias: {alias}\n"
        f"    instance_id: {instance_id}\n"
    )
    if instance_type:
        text += f"    instance_type: {instance_type}\n"
    text += (
        f"    ssh_host: {ssh_host}\n"
        f"    ssh_user: {ssh_user}\n"
        f"    ssh_key: {ssh_key}\n"
        f"    status: {status}\n"
    )
    REGISTRY_PATH.write_text(text)


def remove_boxes(aliases: list[str]) -> int:
    """Drop whole entries by alias. Returns how many were removed."""
    if not REGISTRY_PATH.exists():
        return 0
    lines = REGISTRY_PATH.read_text().splitlines(keepends=True)
    removed = 0
    for alias in aliases:
        try:
            start, end, _ = _entry_span(lines, alias)
        except KeyError:
            continue
        del lines[start:end]
        removed += 1
    REGISTRY_PATH.write_text("".join(lines))
    return removed
