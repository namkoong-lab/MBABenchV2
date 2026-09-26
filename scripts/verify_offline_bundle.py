#!/usr/bin/env python3
"""Check the offline bundle under data/ without any credentials.

  1. every file in data/MANIFEST.json exists with the recorded size and sha256 (the 202 task
     workbooks are not tracked in git: when they are missing, one line says where to download
     the zip and how to install it with scripts/install_task_files.py);
  2. every one of the 101 tasks has a task.json, starting files, solution files and a
     rubric-suitability annotation (judge/rubric_suitability/task_id=N.json);
  3. every prompt / template version referenced by an offline run config exists in its
     pipeline's registry, and the judge's configured versions exist under judge/prompts/.

Exit code 1 on any failure.

    uv run python scripts/verify_offline_bundle.py [--quick]
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / os.environ.get("SPREADSHEETSMITH_DATA_ROOT", "data")
ZIP_URL = "https://anonymous-hf.com/a/v410gr4w0zf8/"
TASK_FILE = re.compile(r"^data/tasks/task_id=\d+/(starting_files|solution_files)/")
PROBLEMS: list[str] = []
NOTES: list[str] = []


def local(rel: str) -> Path:
    """A bundle path (repo-relative, under data/) resolved onto the data root."""
    return DATA / rel[len("data/"):] if rel.startswith("data/") else ROOT / rel


def problem(msg: str) -> None:
    PROBLEMS.append(msg)
    print(f"  FAIL {msg}")


def note(msg: str) -> None:
    NOTES.append(msg)
    print(f"  note {msg}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    """Rows of <path>; of <path>.gz (gzip) when only the compressed copy is in place."""
    gz = path.with_name(path.name + ".gz")
    opener = gzip.open(gz, "rt", encoding="utf-8") if not path.exists() and gz.exists() else open(path, encoding="utf-8")
    with opener as f:
        return [json.loads(line) for line in f if line.strip()]


# ----------------------------------------------------------------------------- 1. manifest
def check_manifest(quick: bool) -> int:
    """Returns the number of task workbooks that are not installed."""
    print("1. MANIFEST.json")
    manifest_path = next((p for p in (DATA / "MANIFEST.json", ROOT / "data" / "MANIFEST.json") if p.exists()), None)
    if manifest_path is None:
        problem("data/MANIFEST.json not found"); return 0
    man = json.loads(manifest_path.read_text())
    ok = workbooks_missing = 0
    for e in man["entries"]:
        p = local(e["path"])
        if not p.exists():
            if TASK_FILE.match(e["path"]):
                workbooks_missing += 1
            else:
                problem(f"missing {e['path']}")
            continue
        if p.stat().st_size != e["size"]:
            problem(f"size {p.stat().st_size} != {e['size']}: {e['path']}"); continue
        if not quick and sha256_of(p) != e["sha256"]:
            problem(f"sha256 mismatch: {e['path']}"); continue
        ok += 1
    if workbooks_missing:
        problem(f"{workbooks_missing} task workbooks not installed — download the zip from {ZIP_URL} (Download ZIP) "
                "and run: uv run python scripts/install_task_files.py ~/Downloads/<name>.zip")
    print(f"  {ok}/{len(man['entries'])} files verified ({'size only' if quick else 'sha256'}), "
          f"{man['bytes'] / 1e6:.1f} MB")
    return workbooks_missing


# ----------------------------------------------------------------------------- 2. tasks
def check_tasks(workbooks_reported: bool) -> dict[int, dict]:
    """workbooks_reported: step 1 already reported the workbooks missing; no per-file FAILs then."""
    print("2. tasks")
    tasks = {t["id"]: t for t in json.loads((DATA / "tasks" / "tasks.json").read_text())}
    live = [t for t in tasks.values() if not t["deprecated"]]
    if len(live) != 101:
        problem(f"{len(live)} live tasks, expected 101")
    files_missing = 0
    for t in live:
        tdir = DATA / "tasks" / f"task_id={t['id']}"
        if not (tdir / "task.json").exists():
            problem(f"task {t['id']}: no task.json")
        for col in ("task_starting_files", "task_solution_files"):
            if not t[col]:
                problem(f"task {t['id']}: {col} is empty")
            for f in t[col]:
                if not local(f).exists():
                    files_missing += 1
                    if not workbooks_reported:
                        problem(f"task {t['id']}: missing {f}")
        suit = ROOT / "judge" / "rubric_suitability" / f"task_id={t['id']}.json"
        if not suit.exists():
            problem(f"task {t['id']}: no rubric-suitability annotation {suit.relative_to(ROOT)}")
    if not (ROOT / "house_standards" / "House_Standards_v1.md").exists():
        problem("house_standards/House_Standards_v1.md missing")
    print(f"  {len(live)} tasks with task.json and suitability annotations"
          + (f"; {files_missing} starting / solution files not installed (see step 1)" if files_missing
             else ", starting files and solution files in place"))
    return tasks


# ----------------------------------------------------------------------------- 4. prompt versions
def _yaml(path: Path) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        problem(f"{path.relative_to(ROOT)}: {e}")
        return {}


def check_prompt_versions() -> None:
    print("3. prompt versions referenced by offline run configs")
    checked = 0

    def registry_versions(path: Path) -> set[str]:
        reg = _yaml(path)
        versions = reg.get("versions", reg) if isinstance(reg, dict) else {}
        return {str(k) for k in versions} if isinstance(versions, dict) else set()

    # GUI / Excel: prompt_version -> tasks_configs/prompts/registry.yaml
    for pkg in ("gui-agents-master", "excel-agents-master"):
        reg_path = ROOT / pkg / "tasks_configs" / "prompts" / "registry.yaml"
        versions = registry_versions(reg_path) if reg_path.exists() else set()
        for cfg in sorted(glob.glob(str(ROOT / pkg / "infra" / "configs" / "run_configs" / "offline" / "*.yaml"))):
            pv = _yaml(Path(cfg)).get("prompt_version")
            if pv is not None:
                checked += 1
                if str(pv) not in versions:
                    problem(f"{Path(cfg).relative_to(ROOT)}: prompt_version {pv} not in {reg_path.relative_to(ROOT)}")
    # CLI: prompt_version -> excel_cli_agent/prompts/ (system vNN + template vN)
    cli_prompts = ROOT / "cli-agents-master" / "excel_cli_agent" / "prompts"
    for cfg in sorted(glob.glob(str(ROOT / "cli-agents-master" / "examples" / "offline" / "*.yaml"))):
        pv = _yaml(Path(cfg)).get("prompt_version")
        if pv is None:
            continue
        checked += 1
        s = str(pv)
        m = re.fullmatch(r"v(\d+)", s)
        if m:  # the set label (v16 = system prompt v16 + its template); the recorded number is 1609
            hits = list(cli_prompts.rglob(f"*v{m.group(1)}*"))
        else:
            sys_v, tpl_v = (s[:2], s[2:].lstrip("0") or "0") if len(s) == 4 else (None, None)
            hits = list(cli_prompts.rglob(f"*v{sys_v}*")) + list(cli_prompts.rglob(f"*v{tpl_v}*")) if sys_v else []
        if not hits:
            problem(f"{Path(cfg).relative_to(ROOT)}: prompt_version {pv} has no files under {cli_prompts.relative_to(ROOT)}")
    # coding: template_version -> coding_agent/prompts or templates
    coding = ROOT / "coding-agents-master" / "coding_agent"
    for cfg in sorted(glob.glob(str(ROOT / "coding-agents-master" / "run_configs" / "offline" / "*.yaml"))):
        doc = _yaml(Path(cfg))
        tv = doc.get("template_version") or (doc.get("prompt") or {}).get("template_version")
        if tv is None:
            continue
        checked += 1
        s = str(tv)
        n = s[-2:].lstrip("0") if len(s) == 3 else s
        if not list(coding.rglob(f"*v{n}*")):
            problem(f"{Path(cfg).relative_to(ROOT)}: template_version {tv} has no v{n} template under {coding.relative_to(ROOT)}")
    # judge: project_configs.yaml single_pass template / rubric files
    jc = _yaml(ROOT / "judge" / "project_configs.yaml")
    sp = jc.get("single_pass", {})
    for key in ("template", "template_path", "prompt_template"):
        if key in sp:
            p = ROOT / "judge" / str(sp[key])
            checked += 1
            if not p.exists():
                problem(f"judge/project_configs.yaml single_pass.{key} -> {sp[key]} missing")
    for name in ("rubric_9.json", "rubric_9_weights.json", "rubric_9_guidance.yaml"):
        checked += 1
        if not (ROOT / "judge" / "prompts" / "rubrics" / name).exists():
            problem(f"judge/prompts/rubrics/{name} missing")
    print(f"  {checked} references checked")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="check sizes only, skip sha256 of the manifest files")
    args = ap.parse_args()
    workbooks_missing = check_manifest(args.quick)
    check_tasks(workbooks_missing > 0)
    check_prompt_versions()
    print(f"\n{'OK' if not PROBLEMS else 'FAILED'}: {len(PROBLEMS)} problem(s), {len(NOTES)} note(s)")
    sys.exit(1 if PROBLEMS else 0)


if __name__ == "__main__":
    main()
