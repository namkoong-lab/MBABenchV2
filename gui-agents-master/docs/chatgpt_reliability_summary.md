# ChatGPT web-UI automation for MBABenchV2 — status & decision needed

**Snapshot as of 2026-08-20.** This is a decision memo, not a spec — check it against the code before relying on it.

**Where we are.** The GUI-automation pipeline runs end-to-end on EC2 for both providers (dispatch → cloud browser → upload the case → send the rubric prompts → download the Excel solution → record to Postgres/S3). **Claude (Opus 4.8) works reliably** and produces complete, rubric-compliant workbooks. **ChatGPT is inconsistent**, for two reasons.

First, driving an automated browser from an AWS cloud IP trips ChatGPT's anti-bot defenses: long responses frequently fail to render ("Content failed to load"). We mitigate that with auto-recovery that clicks "Try again" on its own.

Second, and more fundamental: the non-agentic web UI is **non-deterministic about actually building the file**. Given the same prompt that makes Claude produce a real workbook, ChatGPT sometimes writes *text describing* the model instead of producing a downloadable `.xlsx`, so a share of runs return no usable file. This is a limitation of the surface, not a defect in our code — the pipeline sends the prompts correctly, recovers from the load errors, and flags empty or degraded outputs (`check_output_quality` in `infra/run.py`).

**What we've done since.**

- Both providers' prompts carry an explicit instruction (kept identical, to preserve comparability) requiring an actual downloadable `.xlsx` rather than a text description.
- Downloads no longer depend on the preview-card DOM alone. `_download_via_backend_api` pulls the sandbox bytes straight from ChatGPT's own backend API, with the DOM flow as the fallback — this removed a class of renderer OOM crashes on multi-MB workbooks.
- The run records a quality verdict on every attempt, so a degraded workbook is visible in the data rather than silently graded.
- ChatGPT's **Work mode** (`chatgpt_web.mode: work`, model + effort + speed) is now supported and is what the v1 Sol and v2 Sol configs use. It is the closest thing on the current UI to a surface that reliably produces files.

**The options that remain.** ChatGPT **Agent mode** — the Code Interpreter surface that deterministically built files — has been **removed from this repo** (`plan/code_to_update.md` §2) and is no longer on the table without reinstating that support. So:

1. **Work mode at high effort** — already wired; the open question is whether its file-production rate is good enough at scale. That is an empirical question this memo cannot settle: pull the `agent_failed` and quality-verdict rates for `chatgpt_gpt_5_6_sol_work_ultra` (labelled
`chatgpt_gpt_5_6_sol_work` before the effort/speed rename) out of `task_attempts`.
2. **The official ChatGPT API** — deterministic file output and the most defensible path for a full run, at the cost of a larger build and per-token pricing. Note it stops being a *GUI*-agent result, so it is not directly comparable to the Claude web numbers.
3. **Cleaner network egress** (residential proxy / NAT) — would cut the "Content failed to load" errors, but does nothing for file-building non-determinism.
4. **Reinstate Agent mode** — solves the core problem directly, but means restoring the code that was deliberately deleted, and the wave it produced would be a different cohort from the chat/work runs.

Our read: measure option 1 first, since it costs nothing but a query. If work mode's file-production rate is still short of Claude's, the choice is between the API (option 2) and accepting that the ChatGPT web-UI lane has a structurally lower completion rate that the benchmark should report rather than engineer around.
