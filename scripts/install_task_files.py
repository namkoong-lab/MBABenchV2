#!/usr/bin/env python3
"""Install the task workbooks (the starting and solution files of the 101 tasks) into data/.

They are not tracked in git. Download the archive by hand from
https://anonymous-hf.com/a/v410gr4w0zf8/ ("Download ZIP" near the top of the page; it sits
behind a browser check, so no script can fetch it), then run

    uv run python scripts/install_task_files.py [PATH] [--force]

PATH is the downloaded zip or a folder it was extracted into. When omitted, the newest *.zip
in ~/Downloads that holds a task workbook is used. Every member whose path contains
tasks/task_id=<N>/(starting_files|solution_files)/<name> is installed as
<data root>/tasks/task_id=<N>/.../<name> (data root: $SPREADSHEETSMITH_DATA_ROOT or data,
relative to the repository root) and its sha256 checked against data/MANIFEST.json. Members
the manifest does not list are skipped; the rest of the archive (trimmed task.json copies,
.gitattributes) is ignored - the repository's task.json files stay authoritative. A file
already in place is never overwritten without --force, but both it and the archive's copy are
still hashed and compared with the manifest.

Exit code 1 when a manifest task workbook is still missing, or mismatches, after the install.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / os.environ.get("SPREADSHEETSMITH_DATA_ROOT", "data")
URL = "https://anonymous-hf.com/a/v410gr4w0zf8/"
MEMBER = re.compile(r"(?:^|/)tasks/task_id=(\d+)/(starting_files|solution_files)/(.+)$")
TASK_FILE = re.compile(r"^data/tasks/task_id=\d+/(starting_files|solution_files)/")
HOW_TO = (f'download the zip from {URL} ("Download ZIP" near the top of the page) and run: '
          "uv run python scripts/install_task_files.py ~/Downloads/<name>.zip")


def members(src: Path):
    """Yield (member path, open() -> binary stream) for every file in a zip or a folder."""
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                yield p.relative_to(src).as_posix(), (lambda p=p: open(p, "rb"))
        return
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if not info.is_dir():
                yield info.filename, (lambda i=info: zf.open(i))


def newest_download() -> Path:
    zips = sorted(Path.home().glob("Downloads/*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for z in zips:
        try:
            with zipfile.ZipFile(z) as zf:
                found = any(MEMBER.search(n) for n in zf.namelist())
        except zipfile.BadZipFile:
            continue
        if found:
            print(f"using {z} (newest zip in ~/Downloads with task workbooks)")
            return z
    sys.exit(f"no zip with task workbooks in ~/Downloads: {HOW_TO}")


def stream_sha256(stream, part: Path | None) -> str:
    """sha256 of a stream, 1 MB at a time, also written to `part` when given."""
    h = hashlib.sha256()
    out = open(part, "wb") if part else None
    try:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
            if out:
                out.write(chunk)
    finally:
        if out:
            out.close()
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="the downloaded zip or an extracted folder (default: newest zip in ~/Downloads)")
    ap.add_argument("--force", action="store_true", help="overwrite files that are already in place")
    args = ap.parse_args()
    src = Path(args.path).expanduser() if args.path else newest_download()
    if not src.exists():
        sys.exit(f"{src} does not exist: {HOW_TO}")
    manifest_path = next((p for p in (DATA / "MANIFEST.json", ROOT / "data" / "MANIFEST.json") if p.exists()), None)
    if manifest_path is None:
        sys.exit("data/MANIFEST.json not found; run this from a checkout of the repository")
    manifest = {e["path"]: e for e in json.loads(manifest_path.read_text())["entries"]}
    n = {"installed": 0, "already present": 0, "matching": 0, "mismatching": 0, "on disk mismatching": 0,
         "not in manifest": 0, "rejected": 0}
    for name, opener in members(src):
        m = MEMBER.search(name)
        if not m:
            continue
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or ":" in parts[0] or "\\" in name:
            print(f"  rejected unsafe member path: {name}"); n["rejected"] += 1; continue
        rel = f"data/tasks/task_id={m.group(1)}/{m.group(2)}/{m.group(3)}"
        entry = manifest.get(rel)
        if entry is None:
            print(f"  not in manifest, skipped: {name}"); n["not in manifest"] += 1; continue
        dest = DATA / rel[len("data/"):]
        if dest.exists() and not args.force:
            with opener() as f:
                digest = stream_sha256(f, None)
            n["already present"] += 1
            if digest != entry["sha256"]:
                print(f"  already present; the archive's copy MISMATCHES the manifest: {rel}")
            with open(dest, "rb") as f:
                if stream_sha256(f, None) != entry["sha256"]:
                    n["on disk mismatching"] += 1
                    print(f"  the file already on disk MISMATCHES the manifest (re-run with --force): {rel}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            with opener() as f:
                digest = stream_sha256(f, part)
            if digest == entry["sha256"]:
                part.replace(dest)
                n["installed"] += 1
                print(f"  installed {rel}")
            else:
                part.unlink()
                print(f"  sha256 mismatch, not installed: {name}")
        n["matching" if digest == entry["sha256"] else "mismatching"] += 1
    missing = [p for p in manifest if TASK_FILE.match(p) and not (DATA / p[len("data/"):]).exists()]
    print(f"\ninstalled {n['installed']}, already present {n['already present']}, "
          f"archive copies matching the manifest {n['matching']}, mismatching {n['mismatching']}, "
          f"files on disk mismatching {n['on disk mismatching']}, "
          f"not in manifest (skipped) {n['not in manifest']}, rejected {n['rejected']}; "
          f"manifest task files still missing: {len(missing)}")
    for p in missing[:10]:
        print(f"  missing {p}")
    if len(missing) > 10:
        print(f"  ... and {len(missing) - 10} more")
    if missing and not n["installed"] and not n["already present"]:
        print(f"{src} holds none of the manifest's task workbooks: {HOW_TO}")
    sys.exit(1 if missing or n["mismatching"] or n["on disk mismatching"] else 0)


if __name__ == "__main__":
    main()
