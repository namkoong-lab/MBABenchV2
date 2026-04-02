"""
Web Agent - Browser automation for Claude.ai and ChatGPT web interfaces.

This module provides Playwright-based automation for running tasks through
https://claude.ai or https://chatgpt.com.
"""

from .chatgpt_web_agent import ChatGPTWebAgent
from .claude_web_agent import ClaudeWebAgent, ClaudeWebState
from .browser_manager import WebBrowserManager
from .completion_logger import CompletionLogger
from .file_validator import validate_excel_file
from .task_status import (
    AGENT_STATUSES,
    PIPELINE_STATUSES,
    PipelineError,
    TaskStatus,
)
from .web_agent import WebAgent, WebAgentState

# Backward compatibility alias
ClaudeWebBrowserManager = WebBrowserManager

__all__ = [
    "AGENT_STATUSES",
    "ChatGPTWebAgent",
    "ClaudeWebAgent",
    "ClaudeWebBrowserManager",
    "ClaudeWebState",
    "CompletionLogger",
    "PIPELINE_STATUSES",
    "PipelineError",
    "TaskStatus",
    "WebAgent",
    "WebAgentState",
    "WebBrowserManager",
    "validate_excel_file",
]
