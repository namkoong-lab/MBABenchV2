"""
Claude Web Agent - Automate interactions with Claude.ai web interface.

This agent handles:
1. Navigating to claude.ai/new
2. Handling authentication (if needed)
3. Enabling Extended Thinking and Web Search (configurable)
4. Submitting prompts and capturing responses
5. File attachments
6. Response extraction for grading

Configuration options (in claude_web section):
    enable_extended_thinking: bool (default: True) - Enable Extended Thinking mode
    enable_web_search: bool (default: True) - Enable Web Search capability
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from claude_web_agent.web_agent import WebAgent, WebAgentState, ConversationMessage

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """Result of running a task through Claude.ai."""

    task_name: str
    success: bool
    messages: list  # List of ConversationMessage
    start_time: datetime
    end_time: datetime
    error_msg: Optional[str] = None

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


class ClaudeWebAgent(WebAgent):
    """
    Agent for automating Claude.ai web interface.

    This agent uses Playwright to:
    1. Navigate to claude.ai/new
    2. Submit prompts (with optional file attachments)
    3. Wait for and capture Claude's responses
    4. Extract conversation history for grading
    """

    CLAUDE_BASE_URL = "https://claude.ai"
    CLAUDE_NEW_CHAT_URL = "https://claude.ai/new"

    # Selectors for Claude.ai web interface
    SELECTORS = {
        # Input field
        "chat_input": 'div[contenteditable="true"][data-placeholder]',
        "chat_input_alt": 'div[enterkeyhint="enter"]',
        "chat_textarea": 'fieldset div[contenteditable="true"]',
        # Send button
        "send_button": 'button[aria-label="Send message"]',
        "send_button_alt": 'button:has(svg[viewBox="0 0 32 32"])',
        # Stop button (visible when generating)
        "stop_button": 'button[aria-label="Stop response"]',
        "stop_button_alt": 'button:has-text("Stop")',
        # Response content (for fallback detection)
        "response_content": '[class*="prose"]',
        # File upload
        "attach_button": 'button[aria-label="Add files, connectors, and more"]',
        "file_input": 'input[type="file"]',
        # Response elements
        "assistant_message": "div[data-is-streaming]",
        "message_container": 'div[class*="message"]',
        # Auth elements
        "login_button": 'a:has-text("Log in")',
        "email_input": 'input[type="email"]',
        # Rate limit
        "rate_limit_message": 'text="You\'ve reached"',
        # Model selector
        "model_selector": 'button[data-testid="model-selector-dropdown"]',
        # Extended thinking button (clock icon)
        "extended_thinking_button": 'button[aria-label="Extended thinking"]',
        # Toggle menu button (+ button that opens dropdown)
        # Claude.ai renamed this from "Toggle menu" to "Add files, connectors, and more"
        "toggle_menu_button": 'button[aria-label="Add files, connectors, and more"]',
        # Web search checkbox in the dropdown menu
        "web_search_checkbox": 'div[role="menuitemcheckbox"]:has-text("Web search")',
        # Download button in artifact card
        # Multiple selectors for fallback, matching working run_wsp_task_with_file.py
        "download_button": 'button:has-text("Download")',
        "download_button_aria": '[aria-label="Download"]',
        "download_button_text": 'button:text("Download")',
        "download_button_link": 'a:has-text("Download")',
    }

    def __init__(self, page, config: dict, shutdown_event=None, completion_logger=None):
        """
        Initialize Claude Web Agent.

        Args:
            page: Playwright page instance
            config: Configuration dictionary
            shutdown_event: Optional asyncio.Event for graceful shutdown
            completion_logger: Optional logger for timing/completion tracking
        """
        super().__init__(page, config, shutdown_event, completion_logger)

        # Get agent-specific config
        self.agent_config = config.get("claude_web", {})
        self.max_wait_per_prompt = self.agent_config.get(
            "max_wait_per_prompt_seconds", 1800
        )
        self.check_interval = self.agent_config.get("check_interval_seconds", 2)

    async def navigate_to_new_chat(self) -> bool:
        """
        Navigate to claude.ai/new or project chat to start a fresh conversation.

        If project_id or project_url is configured, opens a new chat within that project.

        Returns:
            True if navigation succeeded
        """
        try:
            # Check for project URL or ID
            project_url = self.agent_config.get("project_url")
            project_id = self.agent_config.get("project_id")

            if project_url:
                # Extract project ID from URL if full URL provided
                if "/project/" in project_url:
                    project_id = (
                        project_url.split("/project/")[1].split("/")[0].split("?")[0]
                    )

            if project_id:
                # Navigate to project's new chat URL
                nav_url = f"{self.CLAUDE_BASE_URL}/project/{project_id}"
                logger.info(f"Navigating to project: {nav_url}...")
            else:
                nav_url = self.CLAUDE_NEW_CHAT_URL
                logger.info(f"Navigating to {nav_url}...")

            await self.page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)  # Allow JS to render the chat UI

            # Check if we need to authenticate
            state = await self.get_state()
            if state == WebAgentState.AUTH_REQUIRED:
                logger.warning("Authentication required - please log in manually")
                return False

            logger.info(f"Successfully navigated to Claude.ai (state: {state.value})")

            # Clear any leftover text or files in the chat input
            await self._clear_chat_input()

            # Configure model + extended thinking
            target_model = self.agent_config.get("model", "opus")
            enable_et = self.agent_config.get("enable_extended_thinking", True)
            if not await self.ensure_model_config(
                model=target_model, extended_thinking=enable_et
            ):
                logger.error("Failed to configure model - aborting")
                return False

            # Set Web Search per config (default: disabled)
            enable_web_search = self.agent_config.get("enable_web_search", False)
            if not await self.ensure_web_search_set(enabled=enable_web_search):
                logger.error("Failed to configure Web Search - aborting")
                return False

            return True

        except Exception as e:
            logger.error(f"Failed to navigate to Claude.ai: {e}")
            return False

    # Model button: exact testid, with two text-based fallbacks for the case
    # where Anthropic renames/drops the testid. :has-text is case-insensitive.
    MODEL_BUTTON_SELECTORS = (
        'button[data-testid="model-selector-dropdown"]',
        'button[aria-haspopup="menu"]:has-text("Opus")',
        'button[aria-haspopup="menu"]:has-text("Sonnet")',
        'button[aria-haspopup="menu"]:has-text("Adaptive")',
    )

    # Adaptive thinking (formerly "Extended thinking" / "Think longer").
    # Claude.ai exposes this as a native HTML
    # ``<input role="switch" aria-label="Adaptive thinking" type="checkbox">``
    # inside the model dropdown. We look the switch up by ARIA role+name —
    # stable across visual refactors — and keep historical labels as
    # fallbacks because Anthropic has renamed this control before.
    AT_SWITCH_NAMES = (
        "Adaptive thinking",
        "Extended thinking",
        "Think longer",
    )

    # Structural fallback: if the switch's role+name lookup doesn't
    # resolve (e.g. the control changes shape), probe for a toggle
    # element inside any menuitem whose visible text mentions "think".
    ET_SWITCH_SELECTORS = (
        'input[role="switch"]',
        'input[type="checkbox"]',
        '[role="switch"]',
    )

    async def _get_model_button(self):
        for sel in self.MODEL_BUTTON_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    return btn
            except Exception:
                continue
        return None

    async def _open_model_dropdown(self) -> bool:
        """Open the model selector dropdown if not already open. Retries once."""
        btn = await self._get_model_button()
        if not btn:
            logger.warning("Model selector dropdown button not found")
            return False
        for attempt in (1, 2):
            if (await btn.get_attribute("aria-expanded")) == "true":
                return True
            try:
                await btn.click()
            except Exception:
                try:
                    await btn.click(force=True)
                except Exception as e:
                    logger.warning(f"Dropdown click attempt {attempt} failed: {e}")
                    continue
            await asyncio.sleep(0.8)
            # Did the menu actually render?
            menu = await self.page.query_selector(
                '[role="menu"]:visible, [role="listbox"]:visible'
            )
            if menu or (await btn.get_attribute("aria-expanded")) == "true":
                return True
        return False

    async def _close_model_dropdown(self) -> None:
        try:
            btn = await self._get_model_button()
            if btn and (await btn.get_attribute("aria-expanded")) == "true":
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
        except Exception:
            pass

    async def _find_thinking_switch(self):
        """Return the Adaptive-thinking switch Locator, or None.

        The dropdown must be open. Looks up the switch via ARIA
        ``role="switch"`` + accessible name, trying historical names
        in order (current label first). This is the primary path.
        """
        for name in self.AT_SWITCH_NAMES:
            try:
                locator = self.page.get_by_role("switch", name=name)
                if (await locator.count()) > 0:
                    first = locator.first
                    if await first.is_visible():
                        return first
            except Exception:
                continue
        return None

    async def _find_extended_thinking_item(self):
        """Return the menuitem container wrapping the thinking switch.

        Preferred path: walk up from the switch located via
        ``_find_thinking_switch``. Falls back to a text-based
        structural scan (menuitem whose visible text mentions "think"
        and which contains a toggle) only if the switch lookup fails.
        """
        # Primary: walk up from the switch to its enclosing menuitem.
        sw = await self._find_thinking_switch()
        if sw:
            try:
                item_handle = await sw.evaluate_handle(
                    'el => el.closest(\'[role="menuitem"], [role="menuitemradio"]\')'
                )
                el = item_handle.as_element() if item_handle else None
                if el and await el.is_visible():
                    return el
            except Exception:
                pass

        # Last-resort structural search.
        try:
            items = await self.page.query_selector_all(
                '[role="menuitem"], [role="menuitemradio"]'
            )
            for item in items:
                try:
                    if not await item.is_visible():
                        continue
                    text = ((await item.text_content()) or "").lower()
                    if "think" not in text:
                        continue
                    has_toggle = await item.query_selector(
                        'input, [role="switch"], [aria-checked], [data-state]'
                    )
                    if has_toggle:
                        return item
                except Exception:
                    continue
        except Exception:
            pass
        return None

    async def _read_extended_thinking_switch(self) -> Optional[bool]:
        """Read the Adaptive-thinking switch state. Dropdown must be open.

        Primary path reads the switch located by ARIA role+name via
        ``is_checked()``. Falls back to locating the menuitem and
        probing its descendants for ``is_checked`` / ``aria-checked`` /
        ``data-state`` only if the direct switch lookup doesn't resolve.
        """
        sw = await self._find_thinking_switch()
        if sw:
            try:
                return await sw.is_checked()
            except Exception:
                pass
            try:
                aria = await sw.get_attribute("aria-checked")
                if aria in ("true", "false"):
                    return aria == "true"
            except Exception:
                pass

        # Fallback: item-level descent (legacy path).
        item = await self._find_extended_thinking_item()
        if not item:
            # Effort-based UI fallback: Claude.ai >= 2025-06 replaced the
            # Extended-thinking toggle with an "Effort" submenu item.
            # "Effort: High" is equivalent to extended thinking enabled.
            try:
                items = await self.page.query_selector_all(
                    '[role="menuitem"], [role="menuitemradio"]'
                )
                for menu_item in items:
                    try:
                        if not await menu_item.is_visible():
                            continue
                        text = ((await menu_item.text_content()) or "").lower()
                        if "effort" in text:
                            result = "high" in text
                            logger.debug(
                                f"Effort-based ET detection: text={text.strip()!r} "
                                f"→ ET={'on' if result else 'off'}"
                            )
                            return result
                    except Exception:
                        continue
            except Exception:
                pass
            return None
        for sel in self.ET_SWITCH_SELECTORS:
            try:
                el = await item.query_selector(sel)
            except Exception:
                continue
            if not el:
                continue
            try:
                return await el.is_checked()
            except Exception:
                pass
            aria = await el.get_attribute("aria-checked")
            if aria in ("true", "false"):
                return aria == "true"
            state = await el.get_attribute("data-state")
            if state in ("checked", "unchecked"):
                return state == "checked"
        return None

    async def _watch_extended_thinking(
        self, stop_event: asyncio.Event, interval: int = 20
    ) -> None:
        """Background watcher — re-enables Extended thinking if claude.ai
        flips it off mid-generation. Polls until ``stop_event`` fires.
        """
        while not stop_event.is_set():
            try:
                await self.ensure_extended_thinking(enabled=True)
            except Exception as e:
                logger.debug(f"ET watcher iteration error: {e}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _log_dropdown_dom(self) -> None:
        """Dump a compact summary of dropdown contents for debugging selector drift."""
        try:
            items = await self.page.query_selector_all(
                '[role="menuitem"], [role="menuitemradio"]'
            )
            summaries = []
            for it in items[:12]:
                try:
                    if not await it.is_visible():
                        continue
                    text = ((await it.text_content()) or "").strip()[:60]
                    has_switch = bool(
                        await it.query_selector(
                            'input, [role="switch"], [aria-checked]'
                        )
                    )
                    summaries.append(f"'{text}'{'[switch]' if has_switch else ''}")
                except Exception:
                    continue
            logger.warning(f"Dropdown menuitems seen: {summaries}")
        except Exception:
            pass

    async def ensure_extended_thinking(self, enabled: bool = True) -> bool:
        """Ensure Extended thinking is ``enabled`` by reading the dropdown switch.

        Opens the model dropdown, reads the switch state, clicks to toggle
        if needed, then closes. Safe to call between prompts — the dropdown
        doesn't touch the composer.

        Returns True when the desired state is reached.
        """
        try:
            if not await self._open_model_dropdown():
                return False

            current = await self._read_extended_thinking_switch()
            if current is None:
                logger.warning("Extended thinking switch not found in dropdown")
                await self._log_dropdown_dom()
                await self._close_model_dropdown()
                return False

            if current == enabled:
                logger.debug(f"Extended thinking already {'on' if enabled else 'off'}")
                await self._close_model_dropdown()
                return True

            # Prefer clicking the switch directly (ARIA role+name anchored).
            # Fall back to a menuitem-level click only if the switch lookup
            # fails or the click itself is intercepted.
            clicked = False
            sw = await self._find_thinking_switch()
            if sw:
                try:
                    await sw.click()
                    clicked = True
                except Exception:
                    try:
                        await sw.click(force=True)
                        clicked = True
                    except Exception as e:
                        logger.debug(f"Switch click failed, will try item: {e}")

            if not clicked:
                item = await self._find_extended_thinking_item()
                if not item:
                    await self._close_model_dropdown()
                    return False
                for sel in self.ET_SWITCH_SELECTORS:
                    try:
                        el = await item.query_selector(sel)
                        if el and await el.is_visible():
                            try:
                                await el.click(force=True)
                            except Exception:
                                await el.click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    try:
                        await item.click()
                        clicked = True
                    except Exception as e:
                        logger.error(f"Failed to click ET menuitem: {e}")
            await asyncio.sleep(0.5)

            final = await self._read_extended_thinking_switch()
            await self._close_model_dropdown()

            if final == enabled:
                logger.info(f"Extended thinking {'enabled' if enabled else 'disabled'}")
                return True
            logger.error(
                f"Extended thinking toggle failed: wanted={enabled}, got={final}"
            )
            return False
        except Exception as e:
            logger.error(f"Error toggling extended thinking: {e}")
            await self._close_model_dropdown()
            return False

    async def ensure_model_config(
        self, model: str = "opus", extended_thinking: bool = True
    ) -> bool:
        """Configure model and extended thinking via the model selector dropdown.

        State source of truth is the switch element inside the dropdown menu,
        not the dropdown button label (Claude.ai no longer includes
        "Extended" in the button text).

        Args:
            model: Target model keyword — ``"opus"`` or ``"sonnet"``.
                   Matched case-insensitively against dropdown item text.
            extended_thinking: Whether extended thinking should be on.

        Returns:
            True if the desired state was reached.
        """
        try:
            model_lower = model.lower()
            # UI dropdown text uses spaces/dots (e.g. "Claude Opus 4.8") while
            # config identifiers use underscores (e.g. "opus_4_8"). Use only the
            # base name for substring matching so both formats work.
            model_selector = model_lower.split("_")[0]
            logger.info(
                f"Configuring model={model_lower}, "
                f"extended_thinking={extended_thinking}..."
            )

            model_btn = await self._get_model_button()
            if not model_btn:
                return False

            btn_text = (await model_btn.text_content() or "").lower()
            current_model_ok = model_selector in btn_text

            # Model selection (if needed) — only driver of the model
            if not current_model_ok:
                if not await self._open_model_dropdown():
                    return False

                items = await self.page.query_selector_all(
                    'div[role="menuitemradio"], div[role="menuitem"], div[role="option"]'
                )
                clicked_model = False
                for item in items:
                    try:
                        text = (await item.text_content() or "").lower()
                        if model_selector in text and await item.is_visible():
                            await item.click()
                            logger.info(f"Selected model: {text.strip()}")
                            clicked_model = True
                            await asyncio.sleep(1)
                            break
                    except Exception:
                        continue

                if not clicked_model:
                    more_item = await self.page.query_selector(
                        '[role="menuitem"]:has-text("More models")'
                    )
                    if more_item and await more_item.is_visible():
                        await more_item.hover()
                        await asyncio.sleep(0.5)
                        sub_items = await self.page.query_selector_all(
                            'div[role="menuitemradio"], div[role="menuitem"]'
                        )
                        for item in sub_items:
                            try:
                                text = (await item.text_content() or "").lower()
                                if model_selector in text and await item.is_visible():
                                    await item.click()
                                    logger.info(
                                        f"Selected model from submenu: "
                                        f"{text.strip()}"
                                    )
                                    clicked_model = True
                                    await asyncio.sleep(1)
                                    break
                            except Exception:
                                continue

                if not clicked_model:
                    logger.warning(f"Could not find model '{model_lower}' in dropdown")
                    await self._close_model_dropdown()
                    return False

                # Re-check model from button label
                model_btn = await self._get_model_button()
                btn_text = (
                    (await model_btn.text_content() or "").lower() if model_btn else ""
                )
                current_model_ok = model_selector in btn_text

            if not current_model_ok:
                logger.error(f"Model not set after selection attempt: got {btn_text!r}")
                return False

            # Delegate Extended thinking to the dedicated helper. Treat an ET
            # configuration failure as NON-FATAL: the model is already
            # correctly selected (verified above) and ET is a secondary
            # preference. Claude.ai periodically relabels/relocates the ET
            # switch (e.g. "Extended / Always uses deep reasoning", which the
            # "think"-based detection misses), and a detection miss must not
            # abort an otherwise-valid run.
            et_ok = await self.ensure_extended_thinking(enabled=extended_thinking)
            if not et_ok:
                logger.warning(
                    f"Could not configure extended_thinking={extended_thinking} "
                    f"for model {model_lower} (the model IS selected); continuing. "
                    f"If ET matters for this run, the dropdown switch detection "
                    f"may need updating for the current Claude.ai UI."
                )

            logger.info(
                f"Model configured: model={model_lower}, "
                f"extended_thinking={'on' if et_ok else 'unconfirmed'}"
            )
            return True

        except Exception as e:
            logger.error(f"Error configuring model: {e}")
            await self._close_model_dropdown()
            return False

    async def ensure_web_search_set(self, enabled: bool = False) -> bool:
        """
        Ensure Web Search is set to the desired state.

        Args:
            enabled: True to enable, False to disable (default: False)

        Returns:
            True if Web Search is in the desired state
        """
        desired = "enabled" if enabled else "disabled"
        try:
            logger.info(f"Checking Web Search status (want: {desired})...")

            # First, open the toggle menu (+ button)
            try:
                menu_btn = self.page.get_by_role(
                    "button", name="Add files, connectors, and more"
                )
                if await menu_btn.is_visible(timeout=3000):
                    await menu_btn.click()
                else:
                    logger.warning("Toggle menu button not visible")
                    return False
            except Exception as e:
                logger.debug(f"Role-based menu selector failed: {e}")
                # Fallback to CSS selector
                menu_btn = await self.page.query_selector(
                    self.SELECTORS["toggle_menu_button"]
                )
                if menu_btn and await menu_btn.is_visible():
                    await menu_btn.click()
                else:
                    logger.warning("Toggle menu button not found")
                    return False

            # Wait for the menu to open. [role=menu][data-open] is the
            # Base UI open signal; catches silent click failures.
            try:
                await self.page.wait_for_selector(
                    '[role="menu"][data-open]', timeout=3000
                )
            except Exception:
                logger.debug("Toggle menu did not show [data-open] in time")

            # Now find the Web Search checkbox
            toggled = False
            try:
                web_search = self.page.get_by_role(
                    "menuitemcheckbox", name="Web search"
                )
                if await web_search.is_visible(timeout=2000):
                    is_checked = (
                        await web_search.get_attribute("aria-checked") == "true"
                    )

                    if is_checked == enabled:
                        logger.info(f"Web Search is already {desired}")
                        await self.page.keyboard.press("Escape")
                        return True

                    # Click to toggle
                    await web_search.click()
                    await asyncio.sleep(0.3)
                    toggled = True
            except Exception as e:
                logger.debug(f"Role-based Web Search selector failed: {e}")

            if not toggled:
                # Fallback to CSS selector
                web_search = await self.page.query_selector(
                    self.SELECTORS["web_search_checkbox"]
                )
                if web_search and await web_search.is_visible():
                    is_checked = (
                        await web_search.get_attribute("aria-checked") == "true"
                    )

                    if is_checked == enabled:
                        logger.info(f"Web Search is already {desired}")
                        await self.page.keyboard.press("Escape")
                        return True

                    await web_search.click()
                    await asyncio.sleep(0.3)
                    toggled = True

            if not toggled:
                logger.error("Web Search checkbox not found")
                await self.page.keyboard.press("Escape")
                return False

            # Final verification: re-open the menu and re-read aria-checked
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            try:
                menu_btn = self.page.get_by_role(
                    "button", name="Add files, connectors, and more"
                )
                if await menu_btn.is_visible(timeout=3000):
                    await menu_btn.click()
                else:
                    menu_btn_fallback = await self.page.query_selector(
                        self.SELECTORS["toggle_menu_button"]
                    )
                    if menu_btn_fallback and await menu_btn_fallback.is_visible():
                        await menu_btn_fallback.click()
                    else:
                        logger.error(
                            "Could not re-open toggle menu to verify Web Search"
                        )
                        return False
                try:
                    await self.page.wait_for_selector(
                        '[role="menu"][data-open]', timeout=3000
                    )
                except Exception:
                    logger.debug("Toggle menu did not show [data-open] during verify")

                verify_el = self.page.get_by_role("menuitemcheckbox", name="Web search")
                if await verify_el.is_visible(timeout=2000):
                    actual = await verify_el.get_attribute("aria-checked") == "true"
                else:
                    verify_el = await self.page.query_selector(
                        self.SELECTORS["web_search_checkbox"]
                    )
                    if not verify_el:
                        logger.error("Web Search checkbox missing during verification")
                        await self.page.keyboard.press("Escape")
                        return False
                    actual = await verify_el.get_attribute("aria-checked") == "true"
            finally:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass

            if actual != enabled:
                logger.error(
                    f"Web Search mismatch — wanted {desired}, "
                    f"observed={'enabled' if actual else 'disabled'}"
                )
                return False

            logger.info(f"Web Search {desired} successfully (verified)")
            return True

        except Exception as e:
            logger.error(f"Error setting Web Search: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def ensure_features_enabled(self) -> bool:
        """Configure model, extended thinking, and web search per config."""
        target_model = self.agent_config.get("model", "opus")
        enable_et = self.agent_config.get("enable_extended_thinking", True)
        model_ok = await self.ensure_model_config(
            model=target_model, extended_thinking=enable_et
        )
        enable_ws = self.agent_config.get("enable_web_search", False)
        ws_ok = await self.ensure_web_search_set(enabled=enable_ws)
        return model_ok and ws_ok

    async def get_state(self) -> WebAgentState:
        """
        Determine current state of Claude.ai interface.

        Returns:
            WebAgentState enum value
        """
        try:
            # Check for rate limiting
            rate_limit = await self.page.query_selector(
                self.SELECTORS["rate_limit_message"]
            )
            if rate_limit:
                return WebAgentState.RATE_LIMITED

            # Check for login requirement
            login_btn = await self.page.query_selector(self.SELECTORS["login_button"])
            if login_btn and await login_btn.is_visible():
                return WebAgentState.AUTH_REQUIRED

            # Check for stop button (means Claude is generating)
            for selector in [
                self.SELECTORS["stop_button"],
                self.SELECTORS["stop_button_alt"],
            ]:
                try:
                    stop_btn = await self.page.query_selector(selector)
                    if stop_btn and await stop_btn.is_visible():
                        return WebAgentState.RUNNING
                except Exception:
                    continue

            # Check if input is available
            for selector in [
                self.SELECTORS["chat_input"],
                self.SELECTORS["chat_input_alt"],
                self.SELECTORS["chat_textarea"],
            ]:
                try:
                    input_field = await self.page.query_selector(selector)
                    if input_field and await input_field.is_visible():
                        return WebAgentState.READY
                except Exception:
                    continue

            return WebAgentState.UNKNOWN

        except Exception as e:
            logger.debug(f"Error getting state: {e}")
            return WebAgentState.UNKNOWN

    async def _find_input_field(self):
        """Find the chat input field."""
        for selector in [
            self.SELECTORS["chat_input"],
            self.SELECTORS["chat_input_alt"],
            self.SELECTORS["chat_textarea"],
        ]:
            try:
                element = await self.page.query_selector(selector)
                if element and await element.is_visible():
                    return element
            except Exception:
                continue
        return None

    async def _clear_chat_input(self):
        """Clear any leftover text or attached files in the chat input."""
        try:
            input_field = await self._find_input_field()
            if not input_field:
                return

            # Clear text content
            text_content = await input_field.text_content()
            if text_content and text_content.strip():
                logger.info("Clearing leftover text from chat input...")
                await input_field.click()
                select_all = "Meta+a" if sys.platform == "darwin" else "Control+a"
                await self.page.keyboard.press(select_all)
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.3)

            # Remove any attached file chips (X button inside file-thumbnail)
            try:
                remove_btns = await self.page.query_selector_all(
                    '[data-testid="file-thumbnail"] button.rounded-full'
                )
                for btn in remove_btns:
                    if await btn.is_visible():
                        logger.info("Removing leftover attached file...")
                        await btn.click()
                        await asyncio.sleep(0.3)
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Error clearing chat input: {e}")

    async def _find_send_button(self):
        """Find the send button."""
        for selector in [
            self.SELECTORS["send_button"],
            self.SELECTORS["send_button_alt"],
        ]:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    return btn
            except Exception:
                continue
        return None

    async def upload_files(self, file_paths: list[str]) -> bool:
        """
        Upload files to the current conversation.

        The Claude.ai UI has a two-step flow:
        1. Click the "+" button (aria-label "Add files, connectors, and more")
           which opens a submenu.
        2. Click "Add files or photos" in the submenu, which triggers the
           browser file chooser.

        Falls back to a hidden ``input[type="file"]`` if available.

        Args:
            file_paths: List of file paths to upload

        Returns:
            True if all uploads succeeded
        """
        if not file_paths:
            return True

        try:
            logger.info(f"Uploading {len(file_paths)} file(s)...")

            # Strategy 1: hidden file input (fastest, no UI clicks needed)
            file_input = await self.page.query_selector(self.SELECTORS["file_input"])
            if file_input:
                await file_input.set_input_files(file_paths)
                await asyncio.sleep(2 + len(file_paths))
                logger.info(f"Uploaded {len(file_paths)} file(s) via file input")
                return True

            # Strategy 2: click "+" button -> "Add files or photos" submenu
            attach_btn = await self.page.query_selector(self.SELECTORS["attach_button"])
            if not attach_btn or not await attach_btn.is_visible():
                logger.error("Could not find attach button (+)")
                return False

            # Click "+" and wait for the menu to actually open.
            # [role=menu][data-open] is the Base UI open signal; catches
            # silent click failures that previously manifested as
            # "could not find Add files or photos".
            await attach_btn.click()
            try:
                await self.page.wait_for_selector(
                    '[role="menu"][data-open]', timeout=3000
                )
            except Exception:
                logger.debug("Attach menu did not show [data-open] in time")

            # Find "Add files or photos" via ARIA role+name (stable across
            # localization / text-content renames), not plain text.
            add_files_item = self.page.get_by_role(
                "menuitem", name="Add files or photos"
            )
            try:
                async with self.page.expect_file_chooser(timeout=5000) as fc:
                    await add_files_item.click(timeout=3000)
                chooser = await fc.value
                await chooser.set_files(file_paths)
            except Exception as e:
                logger.debug(f"Submenu approach failed: {e}")
                # Fallback: try clicking any visible "Add files" text
                try:
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                    await attach_btn.click()
                    await asyncio.sleep(0.5)
                    async with self.page.expect_file_chooser(timeout=5000) as fc:
                        # Try clicking the first menu item with a paperclip icon
                        menu_item = await self.page.query_selector(
                            'div[role="menuitem"]:has-text("file")'
                        )
                        if menu_item:
                            await menu_item.click()
                        else:
                            # Last resort: just click the attach button itself
                            await attach_btn.click()
                    chooser = await fc.value
                    await chooser.set_files(file_paths)
                except Exception as e2:
                    logger.error(f"File upload failed: {e2}")
                    await self.page.keyboard.press("Escape")
                    return False

            # Wait for uploads to complete
            await asyncio.sleep(2 + len(file_paths))
            return True

        except Exception as e:
            logger.error(f"File upload failed: {e}")
            return False

    async def submit_prompt(self, prompt: str, prompt_number: int = 1) -> bool:
        """
        Submit a prompt to Claude.

        Args:
            prompt: The prompt text to submit
            prompt_number: Prompt number for logging

        Returns:
            True if submission succeeded
        """
        try:
            logger.info(f"Submitting prompt #{prompt_number}: {prompt[:100]}...")

            # Find input field
            input_field = await self._find_input_field()
            if not input_field:
                logger.error("Could not find chat input field")
                return False

            # Click to focus
            await input_field.click()
            await asyncio.sleep(0.3)

            # Type the prompt (using keyboard for contenteditable divs)
            await input_field.fill("")  # Clear first
            await asyncio.sleep(0.1)

            # For contenteditable, we may need to use type() instead of fill()
            try:
                await input_field.fill(prompt)
            except Exception:
                # Fallback: use keyboard
                await self.page.keyboard.type(prompt, delay=10)

            await asyncio.sleep(1)  # Let UI register the input before sending

            # Click send button or press Enter
            send_btn = await self._find_send_button()
            if send_btn:
                await send_btn.click()
                logger.info("Clicked send button")
            else:
                # Fallback: press Enter
                await self.page.keyboard.press("Enter")
                logger.info("Pressed Enter to send")

            # Record user message
            self.messages.append(
                ConversationMessage(
                    role="user",
                    content=prompt,
                    timestamp=datetime.now(),
                )
            )

            return True

        except Exception as e:
            logger.error(f"Failed to submit prompt: {e}")
            return False

    async def wait_for_response(self, prompt_number: int = 1) -> Optional[str]:
        """
        Wait for Claude to finish responding and extract the response.

        Args:
            prompt_number: Prompt number for logging

        Returns:
            The response text, or None if failed
        """
        logger.info(f"Waiting for response to prompt #{prompt_number}...")

        elapsed = 0
        saw_running = False

        while elapsed < self.max_wait_per_prompt:
            # Check for shutdown
            if self.shutdown_event and self.shutdown_event.is_set():
                logger.warning("Shutdown signal received")
                return None

            await asyncio.sleep(self.check_interval)
            elapsed += self.check_interval

            state = await self.get_state()

            if state == WebAgentState.RUNNING:
                saw_running = True
                if elapsed % 10 == 0:
                    logger.info(f"   [{elapsed}s] Claude is generating...")
                continue

            if state == WebAgentState.RATE_LIMITED:
                logger.error("Rate limit reached!")
                return None

            if state == WebAgentState.READY:
                if not saw_running:
                    # Haven't seen Claude start yet via stop button
                    # Fallback: check if response content has appeared
                    try:
                        responses = await self.page.query_selector_all(
                            self.SELECTORS["response_content"]
                        )
                        if len(responses) > 1:  # More than just the initial prompt
                            saw_running = True
                            logger.info(
                                f"   [{elapsed}s] Detected response content (fallback)"
                            )
                        elif elapsed % 10 == 0:
                            logger.info(
                                f"   [{elapsed}s] Waiting for Claude to start..."
                            )
                            continue
                    except Exception:
                        if elapsed % 10 == 0:
                            logger.info(
                                f"   [{elapsed}s] Waiting for Claude to start..."
                            )
                        continue

                    if not saw_running:
                        continue

                # Claude finished!
                await asyncio.sleep(1)  # Brief stabilization

                # Verify still ready
                final_state = await self.get_state()
                if final_state == WebAgentState.READY:
                    logger.info(f"Prompt #{prompt_number} completed after {elapsed}s")

                    # Extract response
                    response = await self._extract_last_response()
                    if response:
                        self.messages.append(
                            ConversationMessage(
                                role="assistant",
                                content=response,
                                timestamp=datetime.now(),
                            )
                        )
                    return response

        logger.error(f"Timeout waiting for response to prompt #{prompt_number}")
        return None

    async def _extract_last_response(self) -> Optional[str]:
        """
        Extract the last assistant response from the page.

        Returns:
            The response text or None
        """
        try:
            # Claude.ai uses various selectors for messages
            # Try to find assistant messages
            selectors = [
                'div[data-is-streaming="false"]',
                "div.font-claude-message",
                'div[class*="prose"]',
                "article div",
            ]

            for selector in selectors:
                try:
                    elements = await self.page.query_selector_all(selector)
                    if elements:
                        # Get the last one
                        last_el = elements[-1]
                        text = await last_el.text_content()
                        if text and len(text.strip()) > 0:
                            return text.strip()
                except Exception:
                    continue

            # Fallback: try to extract all text from conversation area
            try:
                conversation = await self.page.query_selector("main")
                if conversation:
                    text = await conversation.text_content()
                    return text.strip() if text else None
            except Exception:
                pass

            return None

        except Exception as e:
            logger.debug(f"Error extracting response: {e}")
            return None

    async def process_all_prompts(self, files_to_upload: list = None) -> bool:
        """
        Process all prompts from config sequentially.

        Args:
            files_to_upload: Optional list of files to upload before prompts

        Returns:
            True if all prompts completed successfully
        """
        prompts = self.config.get("prompts", [])
        if not prompts:
            logger.error("No prompts found in config")
            return False

        if isinstance(prompts, str):
            prompts = [prompts]

        # Upload files first if provided
        if files_to_upload:
            logger.info(f"Uploading {len(files_to_upload)} file(s) before prompts...")
            if not await self.upload_files(files_to_upload):
                logger.error("File upload failed")
                return False
            await asyncio.sleep(5)  # Let uploads settle before first prompt

        # Process each prompt
        logger.info(f"Processing {len(prompts)} prompt(s)...")

        enable_et = self.agent_config.get("enable_extended_thinking", True)

        for i, prompt in enumerate(prompts, 1):
            # Check for shutdown
            if self.shutdown_event and self.shutdown_event.is_set():
                logger.warning(f"Shutdown signal before prompt #{i}")
                return False

            # Pause between prompts (not before the first one)
            if i > 1:
                logger.info("Pausing 5s before next prompt...")
                await asyncio.sleep(5)

            logger.info(f"\n{'='*60}")
            logger.info(f"PROMPT {i}/{len(prompts)}")
            logger.info(f"{'='*60}")

            # Re-assert Extended thinking before every submission — claude.ai
            # resets the toggle on each turn, so we must re-enable each time.
            if not await self.ensure_extended_thinking(enabled=enable_et):
                logger.warning(
                    f"Could not verify Extended thinking state before prompt #{i}"
                )

            # Start prompt logging
            if self.completion_logger:
                self.completion_logger.start_prompt(prompt)

            # Submit prompt
            if not await self.submit_prompt(prompt, i):
                logger.error(f"Failed to submit prompt #{i}")
                if self.completion_logger:
                    self.completion_logger.end_prompt(success=False)
                return False

            # Claude.ai flips the Extended thinking switch off mid-stream;
            # run a watcher during wait_for_response that re-enables it.
            et_stop = asyncio.Event()
            et_task = (
                asyncio.create_task(self._watch_extended_thinking(et_stop))
                if enable_et
                else None
            )
            try:
                response = await self.wait_for_response(i)
            finally:
                et_stop.set()
                if et_task:
                    try:
                        await asyncio.wait_for(et_task, timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        et_task.cancel()
            if response is None:
                logger.error(f"Failed to get response for prompt #{i}")
                if self.completion_logger:
                    self.completion_logger.end_prompt(success=False)
                return False

            logger.info(f"Prompt #{i} completed successfully")
            logger.info(f"Response preview: {response[:200]}...")

            # End prompt logging
            if self.completion_logger:
                self.completion_logger.end_prompt(
                    success=True, response_length=len(response)
                )

        logger.info(f"\nAll {len(prompts)} prompts completed!")
        return True

    async def get_conversation_history(self) -> list[dict]:
        """
        Get the full conversation history.

        Returns:
            List of message dictionaries with 'role' and 'content'
        """
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": (
                    msg.timestamp.isoformat()
                    if msg.timestamp
                    else datetime.now().isoformat()
                ),
            }
            for msg in self.messages
        ]

    async def get_last_response(self) -> Optional[str]:
        """Get the last assistant response."""
        for msg in reversed(self.messages):
            if msg.role == "assistant":
                return msg.content
        return None

    async def download_artifact(
        self, download_dir: Optional[str] = None, timeout: int = 30000
    ) -> Optional[str]:
        """
        Download the artifact from Claude's response.

        This clicks the Download button in the artifact card and waits for
        the download to complete.

        Args:
            download_dir: Directory to save downloaded files. If None, uses browser default.
            timeout: Maximum time to wait for download in milliseconds.

        Returns:
            Path to downloaded file, or None if download failed.
        """
        try:
            logger.info("Looking for Download button in artifact card...")

            # Wait a moment for any UI animations to settle
            await asyncio.sleep(1)

            # Use query_selector like the working run_wsp_task_with_file.py
            # Try multiple selectors in order of preference
            download_selectors = [
                self.SELECTORS["download_button"],  # button:has-text("Download")
                self.SELECTORS["download_button_aria"],  # [aria-label="Download"]
                self.SELECTORS["download_button_text"],  # button:text("Download")
                self.SELECTORS["download_button_link"],  # a:has-text("Download")
            ]

            download_btn = None
            for sel in download_selectors:
                try:
                    download_btn = await self.page.query_selector(sel)
                    if download_btn and await download_btn.is_visible():
                        logger.info(f"Found download button with selector: {sel}")
                        break
                except Exception:
                    pass
                download_btn = None

            if not download_btn:
                logger.warning("Could not find Download button")
                return None

            # Set up download handling
            async with self.page.expect_download(timeout=timeout) as download_info:
                logger.info("Clicking Download button...")
                await download_btn.click()

            download = await download_info.value

            # Determine save path
            if download_dir:
                save_path = Path(download_dir) / download.suggested_filename
                await download.save_as(str(save_path))
            else:
                save_path = Path(download.path())

            logger.info(f"Downloaded artifact to: {save_path}")
            return str(save_path)

        except Exception as e:
            logger.error(f"Failed to download artifact: {e}")
            return None

    async def download_all_artifacts(
        self, download_dir: Optional[str] = None, timeout: int = 30000
    ) -> list[str]:
        """
        Download all artifacts from Claude's response.

        Closes the artifact preview panel first to avoid picking up its
        Download button, then finds in-chat download buttons.

        Args:
            download_dir: Directory to save downloaded files.
            timeout: Maximum time to wait for downloads in milliseconds.

        Returns:
            List of paths to downloaded files.
        """
        downloaded_files = []

        try:
            logger.info("Looking for artifacts to download...")
            await asyncio.sleep(1)

            # Close artifact preview panel if open (its Download button
            # duplicates the in-chat download buttons)
            for close_selector in [
                'button[aria-label="Close artifact"]',
                'button[aria-label="Close"]',
                '[data-testid="close-artifact"]',
            ]:
                try:
                    close_btn = await self.page.query_selector(close_selector)
                    if close_btn and await close_btn.is_visible():
                        await close_btn.click()
                        await asyncio.sleep(1)
                        logger.info(
                            f"Closed artifact preview panel via: {close_selector}"
                        )
                        break
                except Exception:
                    continue
            else:
                # No close button found — try Escape
                try:
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(1)
                except Exception:
                    pass

            # Find download buttons in chat only
            logger.info("Looking for artifact download buttons in chat...")

            download_btns = []

            # Scope search to the chat/conversation area to avoid
            # picking up preview panel buttons
            for selector in [
                'main button:text-is("Download")',  # Inside <main> (chat area)
                'button:text-is("Download")',  # Fallback: anywhere with exact text
            ]:
                try:
                    btns = await self.page.query_selector_all(selector)
                    visible_btns = []
                    for b in btns:
                        if await b.is_visible():
                            visible_btns.append(b)
                    if visible_btns:
                        download_btns = visible_btns
                        logger.info(
                            f"Found {len(visible_btns)} download button(s) via: {selector}"
                        )
                        break
                except Exception:
                    continue

            if not download_btns:
                logger.warning("No download buttons found on page")
            else:
                seen_filenames = set()
                # Use a short timeout per button — if a button is blocked
                # by an overlay (e.g. artifact preview panel), fail fast
                per_btn_timeout = 5000
                for i, btn in enumerate(download_btns):
                    try:
                        logger.info(
                            f"Downloading artifact {i+1}/{len(download_btns)}..."
                        )
                        async with self.page.expect_download(
                            timeout=per_btn_timeout
                        ) as download_info:
                            await btn.click(timeout=per_btn_timeout)

                        download = await download_info.value
                        filename = download.suggested_filename

                        # Skip duplicate downloads (same file from preview panel)
                        if filename in seen_filenames:
                            logger.info(f"Skipping duplicate download: {filename}")
                            await download.cancel()
                            continue
                        seen_filenames.add(filename)

                        if download_dir:
                            save_path = Path(download_dir) / filename
                            await download.save_as(str(save_path))
                        else:
                            save_path = Path(download.path())

                        downloaded_files.append(str(save_path))
                        logger.info(f"Downloaded: {save_path}")

                        await asyncio.sleep(0.5)

                    except Exception as e:
                        logger.warning(f"Failed to download artifact {i+1}: {e}")
                        continue

        except Exception as e:
            logger.error(f"Failed to download artifacts: {e}")

        return downloaded_files


# Backward compatibility
ClaudeWebState = WebAgentState
