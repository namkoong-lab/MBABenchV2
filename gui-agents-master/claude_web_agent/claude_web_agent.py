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
        "attach_button": 'button[aria-label="Attach files"]',
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
        "toggle_menu_button": 'button[aria-label="Toggle menu"]',
        # Web search checkbox in the dropdown menu
        "web_search_checkbox": 'div[role="menuitemcheckbox"]:has-text("Web search")',
        # Download button in artifact card
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

            # Select model if configured (before toggling features)
            await self.ensure_model_selected()

            # Always enable Extended Thinking
            et_ok = await self.ensure_extended_thinking_enabled()
            if not et_ok:
                logger.error("Failed to enable Extended Thinking - aborting")
                return False

            # Set Web Search per config (default: disabled)
            enable_web_search = self.agent_config.get("enable_web_search", False)
            await self.ensure_web_search_set(enabled=enable_web_search)

            return True

        except Exception as e:
            logger.error(f"Failed to navigate to Claude.ai: {e}")
            return False

    # Maps config model names to keyword used for substring matching in the UI.
    # Claude.ai may display models as "Opus 4.6", "opus4.6", "Claude Opus",
    # etc. — we match case-insensitively on this keyword.
    MODEL_KEYWORDS = {
        "opus_4_6": "opus",
        "sonnet_4_6": "sonnet",
        "haiku_4_5": "haiku",
    }

    async def ensure_model_selected(self) -> bool:
        """
        Select a specific Claude model via the model selector dropdown.

        Reads ``claude_web.model`` from config. If not set (null/None),
        skips selection and uses whatever model is currently active.

        Supported values: ``opus_4_6``, ``sonnet_4_6``, ``haiku_4_5``.

        Matching is case-insensitive substring: "opus" matches "Opus 4.6",
        "Claude Opus", "opus4.6", etc.

        Returns:
            True if the desired model is selected (or no model was specified).
        """
        target_model = self.agent_config.get("model")
        if not target_model:
            logger.info("No model specified in config — using current default")
            return True

        keyword = self.MODEL_KEYWORDS.get(target_model)
        if not keyword:
            logger.warning(
                "Unknown model '%s'. Valid options: %s. Using current default.",
                target_model,
                ", ".join(self.MODEL_KEYWORDS.keys()),
            )
            return True

        try:
            logger.info("Selecting model: %s (keyword: %s)", target_model, keyword)

            model_btn = await self.page.query_selector(
                'button[data-testid="model-selector-dropdown"]'
            )
            if not model_btn or not await model_btn.is_visible():
                logger.warning(
                    "Model selector dropdown not found — skipping model selection"
                )
                return False

            # Check if already on the target model (case-insensitive substring)
            btn_text = (await model_btn.text_content()) or ""
            if keyword in btn_text.lower():
                logger.info(
                    "Model '%s' is already selected (button: '%s')",
                    keyword,
                    btn_text.strip(),
                )
                return True

            # Open dropdown
            await model_btn.click()
            await asyncio.sleep(1)

            # Scan all menu items for one whose text contains our keyword
            menu_items = await self.page.query_selector_all('div[role="menuitem"]')
            target_item = None
            for item in menu_items:
                item_text = (await item.text_content()) or ""
                if keyword in item_text.lower():
                    target_item = item
                    break

            if not target_item or not await target_item.is_visible():
                logger.warning(
                    "Model '%s' not found in dropdown — using current default",
                    keyword,
                )
                await self.page.keyboard.press("Escape")
                return False

            await target_item.click()
            await asyncio.sleep(0.5)
            logger.info("Model '%s' selected successfully", keyword)

            # Close dropdown if still open
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error("Error selecting model: %s", e)
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def ensure_extended_thinking_enabled(self) -> bool:
        """
        Ensure Extended Thinking is enabled.
        Uses the model selector dropdown (data-testid="model-selector-dropdown").
        If button already shows "Extended", it's already on.

        Returns:
            True if Extended Thinking is enabled (or was already enabled)
        """
        try:
            logger.info("Checking Extended Thinking status...")

            # Step 1: Find the model selector dropdown button
            model_btn = await self.page.query_selector(
                'button[data-testid="model-selector-dropdown"]'
            )
            if not model_btn or not await model_btn.is_visible():
                logger.warning("Model selector dropdown button not found")
                return False

            # Quick check: if button already shows "Extended", it's on
            btn_text = await model_btn.text_content()
            if btn_text and "Extended" in btn_text:
                logger.info("Extended Thinking is already enabled")
                return True

            # Step 2: Open dropdown and find ET switch
            await model_btn.click()
            logger.info("Opened model selector dropdown")
            await asyncio.sleep(1)

            et_item = await self.page.query_selector(
                'div[role="menuitem"]:has-text("Extended thinking")'
            )
            if not et_item or not await et_item.is_visible():
                logger.warning("Extended Thinking menuitem not found in dropdown")
                await self.page.keyboard.press("Escape")
                return False

            switch = await et_item.query_selector('input[role="switch"]')
            if not switch:
                logger.warning("Extended Thinking switch not found")
                await self.page.keyboard.press("Escape")
                return False

            is_checked = await switch.is_checked()
            if is_checked:
                logger.info("Extended Thinking is already enabled")
                await self.page.keyboard.press("Escape")
                return True

            await switch.click(force=True)
            await asyncio.sleep(0.5)
            logger.info("Extended Thinking enabled successfully")
            await self.page.keyboard.press("Escape")
            return True

        except Exception as e:
            logger.error(f"Error enabling Extended Thinking: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
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
                menu_btn = self.page.get_by_role("button", name="Toggle menu")
                if await menu_btn.is_visible(timeout=3000):
                    await menu_btn.click()
                    await asyncio.sleep(0.5)
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
                    await asyncio.sleep(0.5)
                else:
                    logger.warning("Toggle menu button not found")
                    return False

            # Now find the Web Search checkbox
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
                    logger.info(f"Web Search {desired} successfully")
                    return True
            except Exception as e:
                logger.debug(f"Role-based Web Search selector failed: {e}")

            # Fallback to CSS selector
            web_search = await self.page.query_selector(
                self.SELECTORS["web_search_checkbox"]
            )
            if web_search and await web_search.is_visible():
                is_checked = await web_search.get_attribute("aria-checked") == "true"

                if is_checked == enabled:
                    logger.info(f"Web Search is already {desired}")
                    await self.page.keyboard.press("Escape")
                    return True

                await web_search.click()
                await asyncio.sleep(0.3)
                logger.info(f"Web Search {desired} successfully")
                return True

            logger.warning("Web Search checkbox not found")
            await self.page.keyboard.press("Escape")
            return False

        except Exception as e:
            logger.error(f"Error setting Web Search: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def ensure_features_enabled(self) -> bool:
        """Enable extended thinking and set web search per config."""
        et_ok = await self.ensure_extended_thinking_enabled()
        enable_ws = self.agent_config.get("enable_web_search", False)
        ws_ok = await self.ensure_web_search_set(enabled=enable_ws)
        return et_ok and ws_ok

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

        Args:
            file_paths: List of file paths to upload

        Returns:
            True if all uploads succeeded
        """
        if not file_paths:
            return True

        try:
            logger.info(f"Uploading {len(file_paths)} file(s)...")

            # Find file input or attach button
            file_input = await self.page.query_selector(self.SELECTORS["file_input"])

            if file_input:
                # Direct file input available
                await file_input.set_input_files(file_paths)
            else:
                # Need to click attach button first
                attach_btn = await self.page.query_selector(
                    self.SELECTORS["attach_button"]
                )
                if not attach_btn:
                    logger.error("Could not find file upload mechanism")
                    return False

                # Use file chooser
                async with self.page.expect_file_chooser() as fc:
                    await attach_btn.click()
                chooser = await fc.value
                await chooser.set_files(file_paths)

            # Wait for uploads to complete
            await asyncio.sleep(2 + len(file_paths))
            logger.info(f"Uploaded {len(file_paths)} file(s)")
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

            # Start prompt logging
            if self.completion_logger:
                self.completion_logger.start_prompt(prompt)

            # Submit prompt
            if not await self.submit_prompt(prompt, i):
                logger.error(f"Failed to submit prompt #{i}")
                if self.completion_logger:
                    self.completion_logger.end_prompt(success=False)
                return False

            # Wait for response
            response = await self.wait_for_response(i)
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
