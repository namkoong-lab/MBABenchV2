# Excel Agent Development History
**Period**: September 2025 — March 2026
**Project**: Excel CLI Agent with MCP Integration

---

## Executive Summary

The Excel Agent evolved from a first commit prototype (Sep 2025) through foundation fixes, a streaming/timeout crisis, auto pipeline development, prompt optimization (v1→v10), and packaging — to a production system benchmarking 206 tasks across multiple frontier models. This document traces the full arc from prototype to production.

### Key Milestones
- **Sep 25, 2025**: First commit — basic CLI agent with openpyxl MCP server
- **Nov 17-20, 2025**: Foundation fixes (10% → 57% success rate)
- **Dec 11-29, 2025**: Streaming & timeout crisis resolved
- **Feb 3-7, 2026**: Auto pipeline replaces manual workflow
- **Feb 7-18, 2026**: Prompt optimization v4→v10 (v10 = current best)
- **Mar 19, 2026**: Server refactor & pip-installable packaging
- **Mar 20, 2026**: Multi-provider unification

---

## Chronological Timeline

### Phase 0: Prototype (Sep — Oct 2025)

#### Sep 25, 2025 — First Commit
- Basic CLI agent with openpyxl-based MCP server
- Core loop: read task → call LLM → execute Excel tools → repeat

#### Oct 6 — PDF Context & Configuration
- PDF raw text extraction for task context
- Flexible iteration counts
- Config-based prompting system

#### Oct 13-16 — Observability
- Langfuse integration for tracing and observability
- Token usage tracking per API call

#### Oct 22 — Manual Batch Runner
- `BatchRunner` for running multiple workspaces sequentially
- YAML-based batch configuration

#### Oct 28 — Context Management
- `addexcel` CLI command for adding Excel context files to agent context

#### Nov 12 — CSV Logging
- Comprehensive CSV logging of all API requests (model, tokens, cost, latency)
- Foundation for later cost analysis

---

### Phase 1: Foundation Issues (Nov 17-20, 2025)

The agent evolved from a ~10% baseline success rate to 57% (4/7 workspaces) through systematic debugging and targeted fixes.

#### November 17 — Foundation Issues

**Fix 01: Empty File Root Cause**
- **Problem**: Agent called `create_file` 56 times, destroying all previous work. Only 9 formulas survived out of 1,509 attempts (99.4% waste).
- **Root Cause**: Agent lacked state awareness — didn't know file already existed, repeatedly recreated it.
- **Solution**: Two-layer defense — system prompt education (DESTRUCTIVE warning) + MCP server hard block on duplicate file creation.
- **Impact**: 56 file recreations → 1 file creation, formulas accumulate properly.

**Fix 02: Circular Reference Detection**
- **Problem**: 11.8% of formulas had circular references (e.g., `B1: =B1 * (B2/100)`).
- **Solution**: Added `detect_circular_reference()` to formula_validator.py + auto-validation in `set_cell_formula()`.
- **Impact**: 11.8% → 0% circular reference rate.

#### November 18 — State Management

**Fix 03: Deliverable Formatting**
- **Problem**: All 59 formulas correct, but Column A completely empty (no labels), wrong sheet structure.
- **Solution**: Added "DELIVERABLE FORMATTING & USABILITY" section to system prompt with mandatory label requirements.

**Fix 04: Filename and Formula Tool Usage**
- **Problem**: Agent created wrong filenames and used `edit_cells` for formulas (stored as text, not formula objects).
- **Solution**: Explicit system prompt sections for output filename and formula tool usage rules.
- **Impact**: 0 actual formulas → 9 working formulas.

#### November 19 — Corruption Fixes

**Fix 05: Empty Cover Sheet Removal**
- **Problem**: Excel showed corruption warnings due to empty Cover worksheet.
- **Solution**: Removed "Cover" from default worksheet list, added empty worksheet warning.

**Fix 06: Worksheet Creation Duplication**
- **Problem**: Agent created 19 worksheets instead of 7 (12 duplicates, 36 wasted iterations).
- **Solution**: Two-layer defense matching file creation fix — MCP block + prompt guidance.

**Fix 07: PDF Context Integration**
- Automatic PDF text extraction using pypdf, added to agent context before task execution.

#### November 20 — Formula Quality

**Fix 08: Circular Reference Prevention (Enhanced)**
- **Problem**: Original fix caught direct references but not range-based circulars (e.g., `B2` in `SUM(B1:B3)`).
- **Solution**: Enhanced prompt with range-based examples, WRONG vs CORRECT patterns, pre-formula mental checklist.

**Fix 09: Label vs Formula Distinction**
- **Problem**: Labels stored as formulas (`=@Interest Payment`), placeholder formulas (`=SUM(Interest_Payment_Formula_Here)`).
- **Solution**: Clear tool separation rules + explicit placeholder ban.

**Phase 1 Results**:
- Success rate: 10% → 57% (4/7 workspaces)
- Total cost: $7.79 ($1.95 avg per workspace)
- Formula quality: 0-9 → 16-93 formulas, 0% circular refs, 100% label coverage

---

### Phase 2: Fresh Context Mode (Nov 30, 2025)

- Replaced full tool call history with direct Excel sheet content reload each iteration
- Reduced context bloat and improved token efficiency for long-running tasks
- `fresh_context_mode` flag added to TaskExecutor
- Instead of accumulating every past tool call/response, agent sees current Excel state directly

---

### Phase 3: Streaming & Timeout Crisis (Dec 11-29, 2025)

GPT 5.1 adaptation exposed multiple infrastructure issues:

- **JSONL Parsing**: GPT 5.1 returned concatenated JSON objects (not JSON arrays) — required custom JSONL parser
- **Cloudflare 100s Idle Timeout**: Long API calls killed mid-stream → enabled HTTP streaming to keep connection alive
- **Async Socket Hangs**: Async OpenAI client caused CLOSE-WAIT socket hangs → switched to synchronous client
- **Granular Timeouts**: `httpx.Timeout` for separate read/connect timeouts
- **Hard Timeout**: `signal.alarm` for VM streaming hangs (catches cases where streaming stalls silently)
- **MCP Tool Timeout**: `asyncio.wait_for` wrapper for MCP tool calls
- **MCP Connect Timeout**: Protection against MCP subprocess startup hangs
- **venv Compatibility**: `sys.executable` for MCP subprocess (ensures correct Python in virtualenvs)

---

### Phase 4: Sync Refactor & Multi-Model (Jan 15, 2026)

- Full refactor from async to synchronous implementation for reliable timeout handling
- Gemini 3 Pro integration and test configs
- Naming convention change for worksheets (`model_{name}`)
- Synchronous architecture proved more debuggable and timeout-friendly than async

---

### Phase 5: Auto Pipeline (Feb 3-7, 2026)

Replaced 5-step manual workflow with single-command automation:

**Before (manual)**:
1. Manually create workspace directory
2. Download task files from S3
3. Run batch with workspace paths in YAML
4. Manually upload results to S3
5. Manually create DB entry

**After (auto)**:
```bash
excel-agent --batch-config auto_config.yaml
```

**Key components**:
- **AutoBatchRunner**: DB task discovery → S3 download → execute → S3 upload → DB write
- **Direct cost/time tracking**: From API responses (no CSV re-parsing)
- **Prompt versioning**: `system_v * 100 + template_v`, stored in `task_attempts.prompt_version`
- **Trial management**: Skip tasks after N attempts per model (`max_trials` + `trials_since`)
- **Auto-recalc on edit_cells**: Previously only `set_cell_formula` triggered LibreOffice recalc

---

### Phase 6: Prompt Optimization (Feb 7-18, 2026)

Systematic optimization of system prompt and task templates:

| Version | System Lines | Template Lines | Key Changes | Result |
|---------|-------------|----------------|-------------|--------|
| v1 | 902 | 52 | Baseline — all core rules | 5 iters, 138 formulas, good |
| v2 | 1082 | 102 | +formatting, +sign, +quality, +7-step template | 1 iter mega-batch, 76 formulas |
| v3 | 1020 | 78 | v2 minus formatting section | Still mega-batch, 39 formulas |
| v4 | 1021 | 56 | v1 base + surgical v2 additions | Testing |
| v5 | 1022 | 56 | v4 copy (baseline for v6 comparison) | 6 iters, 47 formulas |
| v6 | 1063 | 56 | Rubric-aligned formatting (8 criteria with HOW hints) | **Best at the time** |
| v7 | 1070 | 56 | v6 + rubric gap fills | Worse — completion checklists caused mega-batching |
| v8 | 1072 | 56 | v6 + gaps relocated to build-time sections | Worse — relocation didn't help |
| v9 | 1068 | 56 | v6 + only formula rules (IFERROR, dynamic ranges) | Worse — even pure formula rules degraded |
| v10 | 866 | 56 | Rubric rewritten near-verbatim from rubric.json (17 criteria) | **Current best** |

**v6-v10 comparison** (3 trials each, `Speed-It-Up-Finance-unhfuz`, GPT 5.2 non-thinking):

| Version | Avg Formulas | Avg format_cells | Avg Cost | Avg Time |
|---------|-------------|-----------------|----------|----------|
| v6 | 246 | 17 | $0.58 | 5.2min |
| v7 | 120 | 23 | $0.19 | 2.1min |
| v8 | 199 | 0 | $0.34 | 3.4min |
| v9 | 71 | 4 | $0.32 | 2.1min |
| **v10** | **356** | **21** | **$0.50** | **4.5min** |

**Key insight**: `reasoning_effort: "none"` for GPT 5.2 lets the model use its full token budget for tool calls instead of burning tokens on internal reasoning. v1-v4 results used default reasoning; v5+ used "none" — results not comparable across groups.

---

### Phase 7: Server Refactor & Packaging (Feb 16 — Mar 19, 2026)

- **Server modularization**: Split monolithic `server.py` (~2400 lines) into `excel_mcp_server/tools/` directory (6 tool files + helpers + core)
- **Model pricing registry**: Extracted to `excel_cli_agent/models_config.py`
- **pip-installable package**: `pip install .` installs both packages, bundled prompts, DB models; provides `excel-agent` CLI command
- **Dockerfile**: Containerized deployment option
- **CI**: GitHub Actions with ruff lint + pytest

---

### Phase 8: Multi-Provider Unification (Mar 20, 2026)

- Unified `base_url` parameter replaces legacy `use_anthropic_direct`, `use_openai_direct` flags
- Auto-detects provider from URL:
  - `anthropic` in URL → extended thinking mode
  - `openrouter` in URL → prefer OpenRouter API key
  - Otherwise → OpenAI-compatible client
- Single parameter supports vLLM, SGLang, OpenRouter, Anthropic, OpenAI

---

## Key Patterns Discovered

### Two-Layer Defense Strategy
**Pattern**: Combine system prompt education with MCP server validation for critical operations.

- **Layer 1 (System Prompt)**: Educates agent on best practices, prevents mistakes up front
- **Layer 2 (MCP Validation)**: Hard enforcement, guarantees agent can't make destructive mistakes

**Applied To**: File creation (prevent overwrites), worksheet creation (prevent duplicates), formula validation (prevent circular references).

**Result**: Agent learns from helpful error messages and adapts behavior without wasting iterations.

### Validation Gap: openpyxl vs Excel
Python library validation doesn't catch everything Excel checks. openpyxl accepts empty worksheets and doesn't check circular references; Excel flags both. **Lesson**: Always test generated files in Microsoft Excel, not just openpyxl.

### Agent State Management
AI agents lack persistent memory across iterations. External validation (MCP server blocks) compensates for lack of internal state tracking. Specific, helpful error messages ("File exists, use edit_cells instead") drive faster adaptation than generic errors.

### Prompt Weight Sensitivity
Heavier prompts (more lines, more rules) don't always improve performance. v2 (1082 lines) caused mega-batching; v10 (866 lines) is the best performer. The task template is especially sensitive — keep under 60 lines. Completion checklists consistently degraded performance by encouraging one-shot mega-batches.

---

## Current Status (March 2026)

### Production Metrics
| Metric | Value |
|--------|-------|
| Total tasks | 206 (148 FMWC + 60 ModelOff + 47 WSP, minus deprecated) |
| Models benchmarked | 9+ via OpenRouter + Anthropic direct |
| Current prompt | v10 (866 system lines, 56 template lines) |
| Cost tracking | Direct from API responses |
| Infrastructure | 3 GCP VMs, auto pipeline, S3 + PostgreSQL |

### Prompt v10 Performance (GPT 5.2 non-thinking, Speed-It-Up-Finance)
| Metric | Average (3 trials) |
|--------|-------------------|
| Formulas set | 356 |
| format_cells calls | 21 |
| Cost | $0.50 |
| Time | 4.5 min |

---

## Appendix: November 17-20 Detailed Fix Log

### Success Rate Evolution
| Date | Success Rate | Key Changes |
|------|--------------|-------------|
| Nov 17 (baseline) | ~10% | Original implementation |
| Nov 18 | ~30% | State management + circular refs fixed |
| Nov 19 | ~45% | Empty worksheets + PDF context fixed |
| Nov 20 | **57%** | Labels + placeholders fixed |

### Cost Evolution (per workspace)
| Configuration | Model | Avg Cost | Success Rate |
|--------------|-------|----------|--------------|
| Baseline | gpt-4o | $3.81 | 71% (false positive) |
| Verbose | gpt-4o | $12.56 | 71% (overthinking) |
| Optimized | gpt-4o-mini | $0.46 | 86% |
| Current (Nov 20) | gpt-4o-mini | $1.95 | 57% (true positive) |

### Formula Quality Improvement
| Metric | Before Fixes | After Fixes | Improvement |
|--------|--------------|-------------|-------------|
| Formula Count | 0-9 | 16-93 | +10x |
| Circular Refs | 11.8% | 0% | -100% |
| Empty Cell Refs | Common | 0% | -100% |
| Label Coverage | 0% | 100% | +100% |
| Placeholder Formulas | Common | 0% | -100% |

---

**Document Version**: 2.0
**Last Updated**: March 21, 2026
**Coverage**: September 2025 — March 2026
