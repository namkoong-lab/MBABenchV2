"""Periodic provider-login check for a single box.

Runs via gui-agents-auth-probe.timer (systemd). Connects to the box's
Chrome over CDP — same Chrome the worker drives — and checks whether a
logged-in session is usable.

Per-provider strategy:
  * chatgpt — hit https://chatgpt.com/api/auth/session via the browser
    context's cookie jar. The endpoint returns `{user, account, expires,
    accessToken, ...}` when authenticated and `{}` for guests. Strictly
    better than a DOM selector: guests now see the composer on the
    landing page, and this endpoint also exposes `account.planType`
    ("pro" / "plus" / "free"), which catches "logged into the wrong
    account tier" before the worker wastes a task on it. NEVER persist
    the `accessToken` / `sessionToken` fields — `auth.json` is surfaced
    over SSH by `dispatch status`.
  * claude — wait on the composer selector (unchanged). claude.ai still
    gates the composer behind login, so the DOM signal is reliable.

Result is written to /var/lib/gui-agents/auth.json and surfaced by
`gui-agents-queue show` so `dispatch status` can read it in one SSH hop.

Skips silently when the worker has a current task — touching the shared
browser mid-task could race with the agent. Next tick picks it up.
(This is conservative; the ChatGPT path uses request.get rather than
opening a page, so we could relax it later.)
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

SUPPORTED_PROVIDERS = ("chatgpt", "claude")

CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session"
CLAUDE_HOME_URL = "https://claude.ai/"
CLAUDE_COMPOSER_SELECTOR = 'fieldset div[contenteditable="true"]'

PROBE_NAV_TIMEOUT_MS = 20_000
PROBE_SELECTOR_TIMEOUT_MS = 10_000
PROBE_REQUEST_TIMEOUT_MS = 15_000


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


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


def _probe_chatgpt(cdp_port: int) -> tuple[bool, str | None, dict]:
    """Query /api/auth/session with the worker's cookies.

    Returns (ok, reason, extra) where `extra` holds the non-sensitive
    identity fields to persist in auth.json.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        try:
            contexts = browser.contexts
            if not contexts:
                return False, "no_browser_context", {}
            ctx = contexts[0]
            try:
                resp = ctx.request.get(
                    CHATGPT_SESSION_URL, timeout=PROBE_REQUEST_TIMEOUT_MS
                )
            except Exception as e:
                return False, f"request_error:{type(e).__name__}", {}
            if resp.status != 200:
                return False, f"http_{resp.status}", {}
            try:
                data = json.loads(resp.text() or "{}")
            except json.JSONDecodeError:
                return False, "non_json_response", {}
            user = data.get("user") or {}
            if not user:
                return False, "no_session", {}
            extra = {
                "email": user.get("email"),
                "plan": (data.get("account") or {}).get("planType"),
                "expires": data.get("expires"),
            }
            return True, None, extra
        finally:
            # Do NOT close the browser — that'd tear down the shared Chrome.
            browser.close()


def _probe_claude_composer(cdp_port: int) -> tuple[bool, str | None, dict]:
    """Open claude.ai, wait for the composer. No identity fields available."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        try:
            contexts = browser.contexts
            context = contexts[0] if contexts else browser.new_context()
            page = context.new_page()
            try:
                try:
                    page.goto(
                        CLAUDE_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=PROBE_NAV_TIMEOUT_MS,
                    )
                except PWTimeoutError:
                    return False, "nav_timeout", {}
                try:
                    page.wait_for_selector(
                        CLAUDE_COMPOSER_SELECTOR,
                        timeout=PROBE_SELECTOR_TIMEOUT_MS,
                    )
                    return True, None, {}
                except PWTimeoutError:
                    final_url = page.url or ""
                    if not final_url.startswith(CLAUDE_HOME_URL.split("?")[0]):
                        return False, f"redirected_to:{final_url[:120]}", {}
                    return False, "composer_not_found", {}
            finally:
                page.close()
        finally:
            browser.close()


def _run_probe(provider: str, cdp_port: int) -> tuple[bool, str | None, dict]:
    if provider == "chatgpt":
        return _probe_chatgpt(cdp_port)
    if provider == "claude":
        return _probe_claude_composer(cdp_port)
    return False, "unsupported_provider", {}


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
    if provider not in SUPPORTED_PROVIDERS:
        logger.error(f"unknown or unsupported provider: {provider!r}")
        _write(
            {
                "provider": str(provider) if provider else None,
                "ok": False,
                "checked_at": _now_iso(),
                "reason": "unsupported_provider",
            }
        )
        return 2

    try:
        cdp_port = _resolve_cdp_port(cfg, provider)
    except Exception as e:
        logger.error(f"could not resolve CDP port: {e}")
        _write(
            {
                "provider": provider,
                "ok": False,
                "checked_at": _now_iso(),
                "reason": f"cdp_port_missing:{type(e).__name__}",
            }
        )
        return 2

    try:
        ok, reason, extra = _run_probe(provider, cdp_port)
    except Exception as e:
        # Connection refused, Chrome not running, etc. Distinct from
        # "logged out" — the operator may want to see the class of error.
        logger.error(f"probe error: {e}")
        _write(
            {
                "provider": provider,
                "ok": False,
                "checked_at": _now_iso(),
                "reason": f"probe_error:{type(e).__name__}",
            }
        )
        return 1

    logger.info(
        f"provider={provider} ok={ok} reason={reason} "
        f"email={extra.get('email')!r} plan={extra.get('plan')!r}"
    )
    payload = {
        "provider": provider,
        "ok": ok,
        "checked_at": _now_iso(),
        "reason": reason,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    _write(payload)
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
