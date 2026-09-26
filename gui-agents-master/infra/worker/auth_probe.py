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
  * claude — hit https://claude.ai/api/account via the browser context's
    cookie jar. Same trade as the chatgpt path: it yields `email_address`
    (so `dispatch status` can name the account) plus the org's
    `capabilities` / `rate_limit_tier`, which catch "logged into the wrong
    tier" — e.g. a free account where a Max one was expected. Falls back
    to the composer selector when that endpoint is unreachable or answers
    something we can't read, so an API change degrades to the old DOM
    signal rather than reporting a false logout. NEVER persist anything
    beyond the whitelisted identity fields — `auth.json` is surfaced over
    SSH by `dispatch status`. (/api/bootstrap carries the same email but
    ships Intercom JWTs alongside it; /api/account does not.)

Result is written to /var/lib/gui-agents/auth.json and surfaced by
`gui-agents-queue show` so `dispatch status` can read it in one SSH hop.

Skips silently when the worker has a current task — touching the shared
browser mid-task could race with the agent. Next tick picks it up.
(This is conservative; both providers now take a request.get path in the
common case rather than opening a page, so we could relax it later — but
claude's composer fallback still opens one.)
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
CLAUDE_ACCOUNT_URL = "https://claude.ai/api/account"
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


def _claude_plan(account: dict) -> str | None:
    """Human-readable tier from the account's first org membership.

    `capabilities` looks like ["chat", "claude_max"] — "chat" is on every
    account, so the tier is whatever else is there. A free account has only
    ["chat"], which is itself the answer ("free").
    """
    memberships = account.get("memberships") or []
    if not memberships:
        return None
    org = (memberships[0] or {}).get("organization") or {}
    caps = [c for c in (org.get("capabilities") or []) if c != "chat"]
    if caps:
        return ",".join(caps)
    return "free" if org.get("capabilities") else None


def _claude_rate_limit_tier(account: dict) -> str | None:
    memberships = account.get("memberships") or []
    if not memberships:
        return None
    org = (memberships[0] or {}).get("organization") or {}
    return org.get("rate_limit_tier")


def _probe_claude_account(ctx) -> tuple[str, str | None, dict]:
    """Query /api/account with the worker's cookies.

    Returns (verdict, reason, extra) where verdict is one of:
      "ok"           — authenticated; `extra` holds the identity fields
      "logged_out"   — the endpoint says there is no session
      "inconclusive" — we couldn't tell (transport error, unexpected shape);
                       the caller should fall back to the DOM check
    """
    try:
        resp = ctx.request.get(CLAUDE_ACCOUNT_URL, timeout=PROBE_REQUEST_TIMEOUT_MS)
    except Exception as e:
        return "inconclusive", f"request_error:{type(e).__name__}", {}
    if resp.status in (401, 403):
        return "logged_out", f"http_{resp.status}", {}
    if resp.status != 200:
        return "inconclusive", f"http_{resp.status}", {}
    try:
        data = json.loads(resp.text() or "{}")
    except json.JSONDecodeError:
        return "inconclusive", "non_json_response", {}
    if not isinstance(data, dict):
        return "inconclusive", "unexpected_response_shape", {}
    if data.get("is_anonymous"):
        return "logged_out", "anonymous_account", {}
    email = data.get("email_address")
    if not email:
        # 200 without an identity is not proof of a logout — the shape may
        # simply have moved. Let the composer check decide.
        return "inconclusive", "no_email_in_response", {}
    return (
        "ok",
        None,
        {
            "email": email,
            "plan": _claude_plan(data),
            "rate_limit_tier": _claude_rate_limit_tier(data),
        },
    )


def _probe_claude_composer(ctx) -> tuple[bool, str | None]:
    """Open claude.ai, wait for the composer. No identity fields available."""
    from playwright.sync_api import TimeoutError as PWTimeoutError

    page = ctx.new_page()
    try:
        try:
            page.goto(
                CLAUDE_HOME_URL,
                wait_until="domcontentloaded",
                timeout=PROBE_NAV_TIMEOUT_MS,
            )
        except PWTimeoutError:
            return False, "nav_timeout"
        try:
            page.wait_for_selector(
                CLAUDE_COMPOSER_SELECTOR,
                timeout=PROBE_SELECTOR_TIMEOUT_MS,
            )
            return True, None
        except PWTimeoutError:
            final_url = page.url or ""
            if not final_url.startswith(CLAUDE_HOME_URL.split("?")[0]):
                return False, f"redirected_to:{final_url[:120]}"
            return False, "composer_not_found"
    finally:
        page.close()


def _probe_claude(cdp_port: int) -> tuple[bool, str | None, dict]:
    """Identity via /api/account, with the composer DOM check as fallback."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        try:
            contexts = browser.contexts
            if not contexts:
                # A fresh browser.new_context() would carry no cookies, so it
                # could never be logged in — reporting that as a logout would
                # be misleading. Name the real problem instead.
                return False, "no_browser_context", {}
            ctx = contexts[0]
            verdict, reason, extra = _probe_claude_account(ctx)
            if verdict == "ok":
                return True, None, extra
            dom_ok, dom_reason = _probe_claude_composer(ctx)
            if dom_ok:
                # Logged in, but we have no identity to show. Keep the
                # endpoint's reason so the operator can see why.
                return True, None, {}
            # Prefer the endpoint's reason when it was definitive.
            return False, (reason if verdict == "logged_out" else dom_reason), {}
        finally:
            # Do NOT close the browser — that'd tear down the shared Chrome.
            browser.close()


def _run_probe(provider: str, cdp_port: int) -> tuple[bool, str | None, dict]:
    if provider == "chatgpt":
        return _probe_chatgpt(cdp_port)
    if provider == "claude":
        return _probe_claude(cdp_port)
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
