# Extending the Excel CLI Agent

This document explains how to extend the excel-cli-agent project with new MCP tools, prompt versions, and model configurations.

## Adding an MCP Tool

1. Open the appropriate file in `excel_mcp_server/tools/` (or `excel_mcp_server/server.py` for legacy reference)
2. Add a new async function decorated with `@mcp.tool()`
3. Import the `mcp` instance from `excel_mcp_server.core.shared_state`
4. Import helpers: `from ..core.workbook_io import _load_workbook, _save_workbook_sync, _get_file_path`
5. Return a JSON string (use `json.dumps()`)
6. Include a docstring with Args/Returns format

Example template:
```python
from ..core.shared_state import mcp
from ..core.workbook_io import _load_workbook, _get_file_path
import json

@mcp.tool()
async def my_new_tool(filename: str, sheet_name: str = "Sheet1") -> str:
    """Brief description of what the tool does.

    Args:
        filename: Excel file name ('.xlsx' added if missing)
        sheet_name: Target worksheet name (default: "Sheet1")

    Returns:
        JSON string with the result
    """
    try:
        file_path = _get_file_path(filename)
        wb = _load_workbook(file_path)
        ws = wb[sheet_name]
        # ... tool logic ...
        result = {"status": "success", "data": "..."}
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"
```

## Adding a Prompt Version

1. Create a new file in `excel_cli_agent/prompts/`:
   - System prompts: `excel_cli_agent/prompts/system_prompt_v{N}.txt`
   - Task templates: `excel_cli_agent/prompts/task_template_{source}_v{N}.txt` (source: `fmwc`, `wsp`)
2. Add an entry to `PROMPT_VERSIONS` in `excel_cli_agent/prompt_versions.py`:
   ```python
   PROMPT_VERSIONS = {
       ...
       "v11": {"system": "system_prompt_v11.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
   }
   ```
3. Set `prompt_version: "v10"` in your batch config YAML
4. Document the version change and rationale
5. Never edit a versioned file in-place once it has been used in production runs

## Adding a Model

1. Add pricing to `excel_cli_agent/models_config.py`:
   ```python
   MODEL_PRICING = {
       # ... existing models ...
       "provider/new-model": {"input": 1.00, "output": 5.00},  # per 1M tokens
   }
   ```
2. If the model needs special parameters, add to `MODEL_DEFAULTS`:
   ```python
   MODEL_DEFAULTS = {
       "provider/new-model": {
           "reasoning_effort": "high",
           "max_completion_tokens": 64000,
       },
   }
   ```
3. Use in your batch config:
   ```yaml
   model: "provider/new-model"
   agent_folder: "openpyxl_provider/new-model"
   ```
4. Verify the model slug works with the provider (OpenRouter, OpenAI, Anthropic)
