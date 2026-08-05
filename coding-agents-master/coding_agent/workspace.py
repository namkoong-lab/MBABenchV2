"""Workspace: the agent's disposable per-attempt desk.

Layout handed to the agent:
    workspace/
      PROMPT.md
      starting_files/<inputs>
      solution.xlsx        (agent must create)

The seeded-file manifest (sha256 of every input) lives OUTSIDE the workspace,
in the attempt dir, so validation can prove the agent produced new work.
"""
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .task_source import TaskSpec


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Attempt:
    attempt_dir: Path      # holds workspace/ + all runner-side artifacts
    workspace: Path
    manifest: dict         # {relative filename: sha256}
    started_at: datetime


def create_attempt(workspaces_root: Path, spec: TaskSpec) -> Attempt:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = f"task{spec.task_id}" if spec.task_id is not None else spec.task_name
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(label))[:60]
    attempt_dir = workspaces_root / f"{safe_label}_{ts}"
    workspace = attempt_dir / "workspace"
    files_dir = workspace / "starting_files"
    files_dir.mkdir(parents=True, exist_ok=False)

    manifest = {}
    for src in spec.starting_files:
        dest = files_dir / src.name
        shutil.copy2(src, dest)
        if dest.stat().st_size == 0:
            raise IOError(f"Seeded file is empty: {dest}")
        manifest[f"starting_files/{src.name}"] = sha256_file(dest)
    if not manifest:
        raise IOError("Workspace seeded with zero files")

    (attempt_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return Attempt(
        attempt_dir=attempt_dir,
        workspace=workspace,
        manifest=manifest,
        started_at=datetime.now(),
    )
