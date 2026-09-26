#!/usr/bin/env python3
"""Replace personal names in workbook file properties (docProps/core.xml: creator,
lastModifiedBy), in cell-comment authorship (xl/comments*.xml: the <author> list and
the "Author:" prefix Excel writes at the start of a comment) and in the threaded-comment
person list (xl/persons/person.xml) with a neutral value, touching nothing else in the
file. --replace NAME (repeatable, never stored in this script) additionally replaces that
exact text wherever it appears in any XML part, for a name that sits in a cell.

The workbook is rewritten at the zip level: every part is copied byte for byte with its
original compression; only docProps/core.xml and the comment parts change. Cells,
formulas, formats and sheet XML are untouched. Run after scripts/export_benchmark_data.py (which fetches the
originals) and before committing; it also refreshes the sha256 entries in
data/MANIFEST.json and data/results/ATTEMPT_FILES_MANIFEST.json.

    uv run python scripts/scrub_workbook_metadata.py [--dry-run] [--replace NAME ...] [PATH ...]
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
NEUTRAL = "SpreadsheetSmith"
TAGS = ("dc:creator", "cp:lastModifiedBy")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scrubbed_core(xml: str) -> tuple[str, bool]:
    changed = False
    for tag in TAGS:
        new, n = re.subn(rf"(<{tag}[^>]*>)(.*?)(</{tag}>)",
                         lambda m: m.group(1) + NEUTRAL + m.group(3) if m.group(2).strip() not in ("", NEUTRAL) else m.group(0),
                         xml, flags=re.S)
        if new != xml:
            xml, changed = new, True
    return xml, changed


def scrubbed_comments(xml: str) -> tuple[str, bool]:
    """Comment authors -> NEUTRAL; the 'Author:' prefix inside each comment text likewise."""
    authors = [a for a in re.findall(r"<author>(.*?)</author>", xml, flags=re.S) if a.strip() and a.strip() != NEUTRAL]
    if not authors:
        return xml, False
    new = re.sub(r"<author>(.*?)</author>", f"<author>{NEUTRAL}</author>", xml, flags=re.S)
    for a in sorted(set(authors), key=len, reverse=True):
        new = new.replace(a.strip() + ":", NEUTRAL + ":")
    return new, new != xml


def _is_comment_part(name: str) -> bool:
    return name.startswith("xl/comments") and name.endswith(".xml")


def scrubbed_persons(xml: str) -> tuple[str, bool]:
    new = re.sub(r'(<person\b[^>]*\bdisplayName=")([^"]*)(")',
                 lambda m: m.group(1) + NEUTRAL + m.group(3) if m.group(2) not in ("", NEUTRAL) else m.group(0), xml)
    return new, new != xml


def scrub(path: Path, dry_run: bool, replace: list[str] = ()) -> bool:
    replacements: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "docProps/core.xml" in names:
            new_core, changed = scrubbed_core(z.read("docProps/core.xml").decode("utf-8"))
            if changed:
                replacements["docProps/core.xml"] = new_core.encode("utf-8")
        for name in names:
            if _is_comment_part(name):
                new_xml, changed = scrubbed_comments(z.read(name).decode("utf-8"))
                if changed:
                    replacements[name] = new_xml.encode("utf-8")
            elif name == "xl/persons/person.xml":
                new_xml, changed = scrubbed_persons(z.read(name).decode("utf-8"))
                if changed:
                    replacements[name] = new_xml.encode("utf-8")
        if replace:
            for name in names:
                if not name.endswith((".xml", ".vml")) or name == "docProps/core.xml":
                    continue
                raw = replacements.get(name) or z.read(name)
                text = raw.decode("utf-8", "ignore")
                new = text
                for r in replace:
                    new = new.replace(r, NEUTRAL)
                if new != text:
                    replacements[name] = new.encode("utf-8")
        if not replacements or dry_run:
            return bool(replacements)
        tmp = path.with_name(path.name + ".scrub")
        with zipfile.ZipFile(tmp, "w") as out:
            for info in z.infolist():
                data = replacements.get(info.filename) or z.read(info)
                out.writestr(info, data, compress_type=info.compress_type)
    os.replace(tmp, path)
    return True


def refresh_manifest(path: Path, key: str, touched: set[str]) -> int:
    if not path.exists():
        return 0
    doc = json.loads(path.read_text())
    n = 0
    for e in doc[key]:
        p = ROOT / e["path"]
        if e["path"] in touched and p.exists():
            e["size"], e["sha256"] = p.stat().st_size, sha256_of(p)
            n += 1
    if n:
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files or directories (default: data/tasks and data/results/attempts)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="append", default=[], metavar="NAME",
                    help="exact text to replace with the neutral value in every XML part (repeatable)")
    args = ap.parse_args()
    roots = [Path(p) for p in args.paths] or [DATA / "tasks", DATA / "results" / "attempts"]
    files = [p for r in roots for p in ([r] if r.is_file() else sorted(r.rglob("*.xls[xm]")))]
    touched: set[str] = set()
    for p in files:
        if scrub(p, args.dry_run, args.replace):
            touched.add(p.resolve().relative_to(ROOT).as_posix())
    print(f"{len(touched)} of {len(files)} workbooks {'would be' if args.dry_run else ''} scrubbed")
    if not args.dry_run and touched:
        a = refresh_manifest(DATA / "MANIFEST.json", "entries", touched)
        b = refresh_manifest(DATA / "results" / "ATTEMPT_FILES_MANIFEST.json", "entries", touched)
        print(f"manifest entries refreshed: MANIFEST.json {a}, ATTEMPT_FILES_MANIFEST.json {b}")


if __name__ == "__main__":
    main()
