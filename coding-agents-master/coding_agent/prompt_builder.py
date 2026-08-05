"""Prompt assembly.

PROMPT.md = system wrapper + task template + workspace file listing.

Task templates:
  v5 — byte-exact copies of the pv1105 CLI-wave templates (frozen; checksummed;
       they reference openpyxl-harness tools that coding agents don't have —
       kept for strict-comparability experiments only).
  v6 — the same structure/requirements adapted for coding agents (default).

prompt_version (DB column) follows the CLI convention system*100 + template:
system_prompt_coding_v1 + template v5 -> 105; + template v6 -> 106.
"""
import hashlib
import re
from pathlib import Path

from .config import PROMPTS_DIR, RunConfig
from .task_source import TaskSpec

# Frozen pv1105 template checksums (must match cli-agents-master originals).
FROZEN_MD5 = {
    "task_template_fmwc_v5.txt": "801ea39f8d573870ca361b95ff866568",
    "task_template_wsp_v5.txt": "233064b2dd2a38980391e282ef613cc7",
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def template_name(task_source: str, version: str) -> str:
    kind = "wsp" if task_source == "wsp" else "fmwc"  # modeloff uses the fmwc template (CLI-wave convention)
    return f"task_template_{kind}_{version}.txt"


def parse_prompt_version(system_name: str, template: str) -> int:
    sys_v = int(re.search(r"_v(\d+)\.txt$", system_name).group(1))
    tpl_v = int(re.search(r"_v(\d+)\.txt$", template).group(1))
    return sys_v * 100 + tpl_v


def prompt_file_paths(cfg: RunConfig, task_source: str) -> tuple[Path, Path]:
    system_path = PROMPTS_DIR / cfg.system_prompt
    tpl_path = PROMPTS_DIR / template_name(task_source, cfg.template_version)
    for p in (system_path, tpl_path):
        if not p.exists():
            raise FileNotFoundError(p)
    if tpl_path.name in FROZEN_MD5 and _md5(tpl_path) != FROZEN_MD5[tpl_path.name]:
        raise RuntimeError(f"{tpl_path.name} no longer matches the frozen pv1105 checksum — do not edit v5 templates")
    return system_path, tpl_path


def build_prompt(cfg: RunConfig, spec: TaskSpec, workspace: Path) -> tuple[str, int]:
    """Write PROMPT.md into the workspace; return (prompt_text, prompt_version)."""
    system_path, tpl_path = prompt_file_paths(cfg, spec.task_source)
    listing = "\n".join(
        f"- starting_files/{p.name} ({p.stat().st_size:,} bytes)"
        for p in sorted((workspace / "starting_files").iterdir())
    )
    prompt = (
        f"{system_path.read_text()}\n"
        f"{tpl_path.read_text()}\n"
        f"WORKSPACE FILES:\n{listing}\n"
    )
    (workspace / "PROMPT.md").write_text(prompt)
    return prompt, parse_prompt_version(system_path.name, tpl_path.name)
