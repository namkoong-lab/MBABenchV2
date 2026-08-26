#!/usr/bin/env python3
"""Provision the OneDrive folder tree the excel-agents engine navigates.

For every matching task in the benchmark DB, ensures

    <onedrive_base_path> / <task_source> / <task_name> / Task /

exists on OneDrive and that the task's template workbook (found among its
S3 starting files by the same rule the runner uses) is uploaded into the
Task folder under its original filename — which is exactly the name the
engine will open. Tasks with no workbook among their starting files still
get their folder chain (the engine creates a blank workbook there).

One-time / occasional operator tool, driven through the same automation
Chrome + Microsoft 365 session as the engine. Watch it run: OneDrive's UI
drifts, and a provisioning mistake is cheap to catch here and expensive to
catch as NAV_FAILED retries later.

Usage (from excel-agents-master/):
    uv run python scripts/provision_onedrive.py --dry-run     # list the plan
    uv run python scripts/provision_onedrive.py               # create+upload
    uv run python scripts/provision_onedrive.py --verify      # check + manifest
    uv run python scripts/provision_onedrive.py --task-sources jp

--verify walks the tree read-only and writes onedrive_manifest.json
(task_id -> path, workbook, verified_at) next to this repo's root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from excel_agent.core.browser_manager import BrowserManager
from excel_agent.core.excel_operations import ExcelOperations
from excel_agent.core.file_manager import FileManager
from excel_agent.core.navigation import Navigation, _normalize_name
from infra.configs import ConfigError, load_configs
from infra.run import _ns_to_dict
from playwright.async_api import async_playwright
from task_io.registry import (
    _resolve_db_url,
    _resolve_from_value_or_env,
    describe_database_target,
)
from task_io.sources.postgres_s3 import (
    BizbenchPostgresS3TaskSource,
    MBABenchV2PostgresS3TaskSource,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("provision_onedrive")

MANIFEST_PATH = _REPO_ROOT / "onedrive_manifest.json"


def _build_source(cfg, task_sources, task_ids):
    """A task source for provisioning: every non-deprecated matching task,
    regardless of attempts, under a provisioning-only pseudo agent name."""
    bench = (getattr(cfg, "benchmark", None) or "v2").lower()
    source_cls = (
        BizbenchPostgresS3TaskSource if bench == "v1" else MBABenchV2PostgresS3TaskSource
    )
    aws_cfg = getattr(cfg, "aws", None)
    return source_cls(
        db_url=_resolve_db_url(cfg),
        scratch_dir=_REPO_ROOT / cfg.paths.scratch_dir,
        agent_model_name="__provisioning__",  # unused: attempt filter is off
        prompt_version=None,
        task_ids=list(task_ids or []),
        task_sources=list(task_sources or []),
        skip_deprecated=True,
        skip_already_attempted=False,
        aws_region=getattr(aws_cfg, "region", None) if aws_cfg else None,
        aws_access_key_id=_resolve_from_value_or_env(
            aws_cfg, "access_key_id", "access_key_id_env", "aws"
        ),
        aws_secret_access_key=_resolve_from_value_or_env(
            aws_cfg, "secret_access_key", "secret_access_key_env", "aws"
        ),
        aws_session_token=_resolve_from_value_or_env(
            aws_cfg, "session_token", "session_token_env"
        ),
    )


async def _visible_row_names(page) -> set[str]:
    """Normalized names of the rows currently listed in the OneDrive view."""
    names: set[str] = set()
    try:
        rows = page.locator('[role="row"]')
        count = await rows.count()
    except Exception:
        return names
    for i in range(min(count, 250)):
        row = rows.nth(i)
        try:
            cells = row.locator("a, button, [title]")
            n = await cells.count()
        except Exception:
            continue
        for j in range(min(n, 8)):
            cell = cells.nth(j)
            try:
                for value in (
                    await cell.get_attribute("title"),
                    await cell.text_content(),
                ):
                    if value and value.strip():
                        names.add(_normalize_name(value))
            except Exception:
                continue
    return names


async def _folder_visible(page, name: str) -> bool:
    await asyncio.sleep(1.0)
    return _normalize_name(name) in await _visible_row_names(page)


async def _create_folder(page, name: str) -> bool:
    """Create a folder named `name` in the current OneDrive view."""
    logger.info(f"   📁 Creating folder: {name}")
    opened = False
    for selector in [
        'span:has-text("Create or upload")',
        'text="Create or upload"',
        'button:has-text("New")',
        '[aria-label="New"]',
    ]:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                await el.click()
                opened = True
                break
        except Exception:
            continue
    if not opened:
        logger.error("   ❌ Could not open the Create/New menu")
        return False
    await asyncio.sleep(1.5)

    clicked_folder = False
    for selector in [
        'span:has-text("Folder")',
        'text="Folder"',
        '[aria-label="Folder"]',
    ]:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                await el.click()
                clicked_folder = True
                break
        except Exception:
            continue
    if not clicked_folder:
        logger.error("   ❌ Could not find the 'Folder' menu item")
        await page.keyboard.press("Escape")
        return False
    await asyncio.sleep(1.5)

    name_input, _loc = await ExcelOperations._find_input(
        page, ['input[type="text"]', 'input[aria-label*="name" i]', "input"]
    )
    if not name_input:
        logger.error("   ❌ Folder-name input did not appear")
        await page.keyboard.press("Escape")
        return False
    await name_input.click()
    await name_input.fill(name)
    await asyncio.sleep(0.5)

    for selector in ['button:has-text("Create")', 'text="Create"']:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                await el.click()
                break
        except Exception:
            continue
    else:
        await page.keyboard.press("Enter")
    await asyncio.sleep(2.5)
    return await _folder_visible(page, name)


async def _enter_or_create_folder(page, name: str, create: bool) -> bool:
    """Double-click into `name`, creating it first when allowed."""
    if not await _folder_visible(page, name):
        if not create:
            logger.error(f"   ❌ Folder missing: {name}")
            return False
        if not await _create_folder(page, name):
            return False
    clicked = await Navigation._try_find_and_click_folder(page, name)
    if not clicked:
        logger.error(f"   ❌ Could not open folder: {name}")
        return False
    await asyncio.sleep(1.5)
    return True


async def _upload_file(page, local_path: Path) -> bool:
    """Upload one local file into the current OneDrive folder."""
    logger.info(f"   📤 Uploading: {local_path.name}")
    opened = False
    for selector in [
        'span:has-text("Create or upload")',
        'text="Create or upload"',
        'button:has-text("Upload")',
        '[aria-label="Upload"]',
    ]:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                await el.click()
                opened = True
                break
        except Exception:
            continue
    if not opened:
        logger.error("   ❌ Could not open the Create/Upload menu")
        return False
    await asyncio.sleep(1.5)

    item = None
    for selector in [
        'span:has-text("Files upload")',
        'text="Files upload"',
        'span:has-text("File upload")',
        'text="Files"',
    ]:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                item = el
                break
        except Exception:
            continue
    if not item:
        logger.error("   ❌ Could not find the file-upload menu item")
        await page.keyboard.press("Escape")
        return False

    try:
        async with page.expect_file_chooser(timeout=10000) as fc:
            await item.click()
        chooser = await fc.value
        await chooser.set_files([str(local_path)])
    except Exception as e:
        logger.error(f"   ❌ File chooser failed: {e}")
        return False

    # Give the upload time to land, then confirm the row exists.
    for _ in range(30):
        await asyncio.sleep(2)
        if await _folder_visible(page, local_path.name):
            logger.info(f"   ✅ Uploaded: {local_path.name}")
            return True
    logger.error(f"   ❌ Upload not visible after waiting: {local_path.name}")
    return False


async def _navigate_home(page) -> bool:
    try:
        await page.goto("https://onedrive.live.com", wait_until="load", timeout=60000)
        await asyncio.sleep(3)
        return True
    except Exception as e:
        logger.error(f"❌ Could not load OneDrive: {e}")
        return False


async def provision(args) -> int:
    try:
        cfg = load_configs()
    except ConfigError as e:
        logger.error(f"Config load failed:\n{e}")
        return 2

    base_path = list(_ns_to_dict(cfg).get("onedrive_base_path") or [])
    if not base_path:
        logger.error("onedrive_base_path is empty in the config")
        return 2

    logger.info(f"Database: {describe_database_target(cfg)}")
    logger.info(f"OneDrive base path: {' > '.join(base_path)}")

    source = _build_source(cfg, args.task_sources, args.task_ids)
    try:
        specs = list(source.iter_tasks())
    finally:
        source.close()
    if not specs:
        logger.warning("No tasks matched.")
        return 0

    plan = []
    for spec in specs:
        task_source = (spec.metadata or {}).get("task_source") or ""
        local_paths = [str(p) for p in spec.upload_files]
        workbook = FileManager.find_workbook_file(local_paths, task_source)
        plan.append((spec, task_source, Path(workbook) if workbook else None))

    logger.info(f"\nPlan: {len(plan)} task folder(s) under {' > '.join(base_path)}")
    for spec, task_source, workbook in plan:
        wb = workbook.name if workbook else "(no workbook — folder only)"
        logger.info(f"  {task_source}/{spec.task_name}/Task/  <- {wb}")
    if args.dry_run:
        logger.info("[DRY RUN] Nothing was created or uploaded.")
        return 0

    if not args.yes:
        try:
            answer = input(
                f"\n{'Verify' if args.verify else 'Provision'} "
                f"{len(plan)} task folder(s) on OneDrive? [y/N]: "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            logger.info("Aborted by user.")
            return 0

    browser_cfg = {"browser": _ns_to_dict(getattr(cfg, "browser", None)) or {}}
    browser_mgr = BrowserManager(browser_cfg)
    manifest: dict = {}
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text())
        except Exception:
            manifest = {}

    ok = failed = 0
    async with async_playwright() as playwright:
        _browser, context = await browser_mgr.launch_browser(playwright)
        page = await context.new_page()
        try:
            for spec, task_source, workbook in plan:
                logger.info(f"\n▶ {task_source}/{spec.task_name}")
                if not await _navigate_home(page):
                    failed += 1
                    continue
                segments = base_path + [task_source, spec.task_name, "Task"]
                reached = True
                for segment in segments:
                    if not await _enter_or_create_folder(
                        page, segment, create=not args.verify
                    ):
                        reached = False
                        break
                if not reached:
                    failed += 1
                    continue

                workbook_ok = True
                if workbook is not None:
                    present = await _folder_visible(page, workbook.name)
                    if present:
                        logger.info(f"   ✅ Workbook already present: {workbook.name}")
                    elif args.verify:
                        logger.error(f"   ❌ Workbook missing: {workbook.name}")
                        workbook_ok = False
                    else:
                        workbook_ok = await _upload_file(page, workbook)

                if workbook_ok:
                    ok += 1
                    manifest[str(spec.task_id)] = {
                        "path": segments,
                        "workbook": workbook.name if workbook else None,
                        "verified_at": datetime.now().isoformat(),
                    }
                else:
                    failed += 1
        finally:
            try:
                if not page.is_closed() and len(context.pages) > 1:
                    await page.close()
            except Exception:
                pass
            await browser_mgr.close_browser(context)

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    logger.info(f"\nDone. ok={ok} failed={failed}. Manifest: {MANIFEST_PATH}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision (or verify) the OneDrive task tree from the DB + S3"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan; touch nothing.")
    parser.add_argument("--verify", action="store_true",
                        help="Read-only: check folders + workbooks, write the manifest.")
    parser.add_argument("--task-sources", nargs="*", default=None,
                        help="Restrict to these task_source values (e.g. jp).")
    parser.add_argument("--task-ids", nargs="*", type=int, default=None,
                        help="Restrict to these task ids.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args()
    return asyncio.run(provision(args))


if __name__ == "__main__":
    sys.exit(main())
