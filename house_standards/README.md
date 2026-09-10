# House Standards

The house financial-modelling conventions handed to every agent on a
**benchmark v2** attempt, alongside the task's starting files. This directory
is the single source of truth; every pipeline reads the file from here at run
time and delivers it under its own filename (`House_Standards_v1.md`), which
is the name the prompt directive uses.

| Version | File | Introduced in |
|---|---|---|
| 1 | `House_Standards_v1.md` | gui/excel prompt versions 204/205, cli v14, coding template v10 (2026-09-10) |

## Rules

- **Append-only.** A version that has been sent on a recorded run is
  immutable: `task_attempts` rows point at the prompt version that attached
  it, and rewriting the text under a live number silently changes what that
  history means. New text = `House_Standards_v<n+1>.md` + new prompt
  versions in every pipeline that reference it by name.
- **The prompt version selects the attachment.** GUI and Excel declare it
  under `attachments:` in `tasks_configs/prompts/registry.yaml`; CLI and
  coding declare it on the prompt-version / template stanza. A run config
  never names the file directly, so the recorded `prompt_version` and the
  standards the agent saw cannot disagree.
- **Provenance.** Pipelines that write `task_attempts.extra_configs`
  (cli, coding, excel) record `house_standards: {version, file, sha256}`;
  pipelines that upload a prompt snapshot to S3 upload the file with it.
  GUI records the attachment text in the per-attempt `prompts_*.json`.
- Precedence, as stated in the prompts: case instructions and the prompt
  (including the rubric) govern over the house standards; departures are
  noted on the cover.

## Known interactions

- The CLI validator whitelist must include every function the standards
  recommend (`XLOOKUP`, `XMATCH`, `IFS`, `SWITCH`, `LET`); LibreOffice
  24.8+ evaluates all of them.
- The coding sandbox image (Debian bookworm) ships LibreOffice 7.4, which
  cannot evaluate `XLOOKUP`/`XMATCH`/`LET`; attempts that use them need the
  judge's `--run-calculation` (local LibreOffice 25.8) for cached values.
