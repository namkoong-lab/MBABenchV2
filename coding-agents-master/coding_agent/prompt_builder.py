"""Prompt assembly.

PROMPT.md = system wrapper + task template + workspace file listing.

Task templates:
  v5 — byte-exact copies of the pv1105 CLI-wave templates (frozen; checksummed;
       they reference openpyxl-harness tools that coding agents don't have —
       kept for strict-comparability experiments only).
  v6 — the CLI-wave structure/requirements adapted for coding agents.
  v7 — mirror of the GUI wave's pv9 prompt (DEFAULT): the byte-exact pv9 rubric
       preamble (all 17 grading criteria) + the pv9 three-step closing
       (Summary sheet -> model -> Answers sheet) with only harness-necessitated
       edits (workspace/solution.xlsx wording; the "no code interpreter" ban
       becomes "code may build the workbook, but every calculated value must be
       a live Excel formula"). Task-invariant like the GUI wave: one shared
       template for fmwc/modeloff/wsp.

prompt_version (DB column) follows the CLI convention system*100 + template:
system_prompt_coding_v1 + v5 -> 105; + v6 -> 106; + v7 -> 107.
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

# v7 embeds the GUI wave's pv9 rubric preamble byte-exact; guard its first
# 11,971 bytes (md5 of SHARED_rubric_preamble.txt) against accidental edits.
V7_PREAMBLE_MD5 = "7b12403b2dea9ec6d89d333be819ab52"
V7_PREAMBLE_LEN = 11971

# v8 (benchmark v2) embeds the v2 132-check rubric byte-exact from
# gui-agents-master/tasks_configs/prompts_v2/step2_build.txt ("== FULL
# RUBRIC" onward). Regenerate the template with tools/build_v8_template.py;
# never edit the rubric section by hand.
V8_RUBRIC_MD5 = "ff2e3d483da45b722ed1a39db07d1c14"
V8_RUBRIC_LEN = 60470
V8_RUBRIC_MARKER = "== FULL RUBRIC"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def template_name(task_source: str, version: str) -> str:
    if version in ("v7", "v8"):  # task-invariant, like the GUI prompt sets
        return f"task_template_shared_{version}.txt"
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
    if tpl_path.name == "task_template_shared_v7.txt":
        import hashlib as _h
        head = tpl_path.read_bytes()[:V7_PREAMBLE_LEN]
        if _h.md5(head).hexdigest() != V7_PREAMBLE_MD5:
            raise RuntimeError("v7 rubric preamble no longer matches the GUI pv9 original — do not edit the preamble section")
    if tpl_path.name == "task_template_shared_v8.txt":
        import hashlib as _h
        text = tpl_path.read_text()
        idx = text.index(V8_RUBRIC_MARKER)
        rubric = text[idx:idx + V8_RUBRIC_LEN]
        if _h.md5(rubric.encode()).hexdigest() != V8_RUBRIC_MD5:
            raise RuntimeError(
                "v8 rubric section no longer matches the v2 GUI original — "
                "regenerate with tools/build_v8_template.py instead of editing"
            )
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
