"""Launch the persistent Chrome that the worker drives over CDP.

Runs as gui-agents-chrome.service on each EC2 box. Reads the box's
configs.yaml to pick the active provider's profile_dir + cdp_port, then
execs Chrome in the current process so systemd PID-tracks Chrome
directly. Keeps the browser outside the worker's (and each task unit's)
cgroup, so Chrome survives worker restarts and task-unit teardowns and
has time to flush cookies on stop.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.configs import load_configs  # noqa: E402

logger = logging.getLogger("chrome_launcher")

CHROME_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome-canary",
    "/usr/bin/google-chrome-unstable",
)


def _find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    raise SystemExit(f"no Chrome binary found; looked in {CHROME_CANDIDATES}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_configs()
    provider = cfg.provider.kind
    if provider not in ("claude", "chatgpt"):
        raise SystemExit(
            f"configs.provider.kind must be 'claude' or 'chatgpt', got {provider!r}"
        )

    block = getattr(cfg, f"{provider}_web", None)
    if block is None:
        raise SystemExit(f"configs has no {provider}_web block")

    profile_dir = os.path.expanduser(block.browser.profile_dir)
    cdp_port = int(block.browser.cdp_port)
    os.makedirs(profile_dir, exist_ok=True)

    chrome = _find_chrome()
    args = [
        chrome,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--remote-allow-origins=*",
    ]
    logger.info(
        f"exec chrome: provider={provider} port={cdp_port} profile={profile_dir}"
    )
    os.execvp(chrome, args)


if __name__ == "__main__":
    sys.exit(main())
