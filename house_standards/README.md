# House Standards

The house financial-modelling conventions handed to every agent on a
**benchmark v2** attempt, alongside the task's starting files. This directory
is the single source of truth: `House_Standards_v1.md` is the current version.

## How pipelines attach it

- **The prompt version selects the attachment, never the run config.** GUI and
  Excel declare it under `attachments:` in `tasks_configs/prompts/registry.yaml`
  and upload it with the starting files as `House_Standards_v1.md`. CLI and
  coding declare it on the prompt-version / template stanza and deliver it in
  the workspace as `HOUSE_STANDARDS.md`. So the recorded `prompt_version` and
  the standards the agent saw cannot disagree.
- The coding pipeline stages its own copy,
  `coding-agents-master/coding_agent/prompts/house_standards_v1.md`; its smoke
  test asserts that copy is byte-identical to the file here. Edit here, then copy.
- **Provenance.** Pipelines that write `task_attempts.extra_configs` (cli,
  coding, excel) record `house_standards: {version, file, sha256}`; GUI records
  the attachment text in the per-attempt `prompts_*.json`.
- Precedence, as stated in the prompts: case instructions and the prompt govern
  over the house standards; departures are noted on the cover.

## Rules

- **Append-only.** A version that has been sent on a recorded run is immutable.
  New text = `House_Standards_v<n+1>.md` plus new prompt versions in every
  pipeline that reference it by name.
- The CLI validator whitelist must include every function the standards
  recommend (`XLOOKUP`, `XMATCH`, `IFS`, `SWITCH`, `LET`); LibreOffice 24.8+
  evaluates all of them. The coding sandbox image ships an older LibreOffice
  that cannot; attempts that use them need the judge's `--run-calculation`.
