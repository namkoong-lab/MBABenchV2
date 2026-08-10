# Contributing to excel-cli-agent

An Excel automation agent for financial modeling benchmarks using an MCP server with openpyxl and LibreOffice.

## Prerequisites

- Python 3.10+
- LibreOffice with UNO bindings (required for formula recalculation)
- [uv](https://github.com/astral-sh/uv) package manager

## Dev Setup

```bash
git clone <repo-url>
cd biz_bench_agentic_workflow

pip install -e ".[dev,langfuse]"

cp .env.example .env
# Fill in your API keys (OpenRouter, Anthropic, OpenAI, etc.)
```

## Running Tests

Tests live in the `tests/` directory. Run them with:

```bash
pytest
```

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and formatting.

- Line length: 120
- Target: Python 3.10

```bash
ruff check .
ruff format .
```

Fix auto-fixable lint issues:

```bash
ruff check . --fix
```

## PR Process

1. Create a feature branch from `main`.
2. Make your changes and ensure all tests pass with `pytest`.
3. Run `ruff check .` and `ruff format .` before committing.
4. Submit a PR with a clear description of what changed and why.

## Prompt Versioning System

Prompts are stored as versioned text files in `excel_cli_agent/prompts/` and are bundled as package data. The versioning scheme is strict to ensure reproducibility of benchmark runs.

### File naming

- System prompts: `excel_cli_agent/prompts/system_prompt_v{N}.txt`
- Task templates: `excel_cli_agent/prompts/task_template_{source}_v{N}.txt` (where `{source}` is `fmwc`, `wsp`, etc.)

### Version tracking

The combined prompt version stored in the database is computed as:

```
prompt_version = system_v * 100 + template_v
```

For example, system prompt v4 paired with task template v4 yields prompt version 404.

### Rules

- **Never edit a versioned file in-place** once it has been used in a production run. Results must remain reproducible against the exact prompt text that generated them.
- Create new versions as new files with incremented version numbers (e.g., `system_prompt_v5.txt`).
- Register every new version in the `PROMPT_VERSIONS` dict in `excel_cli_agent/prompt_versions.py` so the pipeline can resolve it.
