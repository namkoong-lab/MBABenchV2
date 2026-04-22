"""Periodic provider-login check for a single box.

Runs via gui-agents-auth-probe.timer (systemd). Connects to the box's
Chrome over CDP — same Chrome the worker drives — opens a throwaway page,
and checks whether the provider's composer selector is present. That's
the same "logged in" signal the worker itself relies on at task start.

Result is written to /var/lib/gui-agents/auth.json and surfaced by
`gui-agents-queue show` so `dispatch status` can read it in one SSH hop.

Skips silently when the worker has a current task — touching the shared
browser mid-task could race with the agent. Next tick picks it up.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.configs import load_configs  # noqa: E402
from infra.worker import state as S  # noqa: E402

logger = logging.getLogger("auth_probe")

AUTH_STATE_PATH = S.STATE_DIR / "auth.json"

# Per-provider probe config: (home_url, composer_selector).
# Composer presence mirrors the signal the worker uses at task start:
# claude_web_agent.py / chatgpt_web_agent.py both wait on these selectors.
PROBES: dict[str, tuple[str, str]] = {
    "chatgpt": (
        "https://chatgpt.com/",
        'div.ProseMirror[contenteditable="true"]',
    ),
    "claude": (
        "https://claude.ai/",
        'fieldset div[contenteditable="true"]',
    ),
}

PROBE_NAV_TIMEOUT_MS = 20_000
PROBE_SELECTOR_TIMEOUT_MS = 10_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write(payload: dict) -> None:
    AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, AUTH_STATE_PATH)


def _resolve_cdp_port(cfg, provider: str) -> int:
    block = getattr(cfg, f"{provider}_web", None)
    if block is None:
        raise ValueError(f"configs has no block for provider: {provider}")
    return int(block.browser.cdp_port)


def _run_probe(cdp_port: int, home_url: str, selector: str) -> tuple[bool, str | None]:
    """Open a throwaway page, wait for `selector`. Returns (ok, reason)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        try:
            # Reuse the existing context so we inherit the worker's cookies.
            contexts = browser.contexts
            context = contexts[0] if contexts else browser.new_context()
            page = context.new_page()
            try:
                try:
                    page.goto(home_url, wait_until="domcontentloaded", timeout=PROBE_NAV_TIMEOUT_MS)
                except PWTimeoutError:
                    return False, "nav_timeout"
                try:
                    page.wait_for_selector(selector, timeout=PROBE_SELECTOR_TIMEOUT_MS)
                    return True, None
                except PWTimeoutError:
                    # Distinguish "redirected away" from "selector never appeared".
                    final_url = page.url or ""
                    if not final_url.startswith(home_url.split("?")[0]):
                        return False, f"redirected_to:{final_url[:120]}"
                    return False, "composer_not_found"
            finally:
                page.close()
        finally:
            # Do NOT close the browser — that'd tear down the shared Chrome.
            browser.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Skip while the worker is busy — opening a new page in the shared
    # context could race with agent interactions.
    s = S.read_state()
    if s.current is not None:
        logger.info(f"worker busy (task={s.current.task_id}); skipping probe")
        # Intentionally do not overwrite the last result; staleness shows
        # up in the dispatcher via checked_at age.
        return 0

    cfg = load_configs()
    provider = getattr(getattr(cfg, "provider", None), "kind", None)
    if provider not in PROBES:
        logger.error(f"unknown or unsupported provider: {provider!r}")
        _write({
            "provider": str(provider) if provider else None,
            "ok": False,
            "checked_at": _now_iso(),
            "reason": "unsupported_provider",
        })
        return 2

    home_url, selector = PROBES[provider]
    try:
        cdp_port = _resolve_cdp_port(cfg, provider)
    except Exception as e:
        logger.error(f"could not resolve CDP port: {e}")
        _write({
            "provider": provider,
            "ok": False,
            "checked_at": _now_iso(),
            "reason": f"cdp_port_missing:{type(e).__name__}",
        })
        return 2

    try:
        ok, reason = _run_probe(cdp_port, home_url, selector)
    except Exception as e:
        # Connection refused, Chrome not running, etc. Distinct from
        # "logged out" — the operator may want to see the class of error.
        logger.error(f"probe error: {e}")
        _write({
            "provider": provider,
            "ok": False,
            "checked_at": _now_iso(),
            "reason": f"probe_error:{type(e).__name__}",
        })
        return 1

    logger.info(f"provider={provider} ok={ok} reason={reason}")
    _write({
        "provider": provider,
        "ok": ok,
        "checked_at": _now_iso(),
        "reason": reason,
    })
    return 0 if ok else 1


def read_auth() -> dict | None:
    """Unlocked read for display (used by gui-agents-queue show)."""
    if not AUTH_STATE_PATH.exists():
        return None
    try:
        return json.loads(AUTH_STATE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    sys.exit(main())
