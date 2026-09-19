# House Standards

The house financial-modelling conventions handed to every agent on a
**benchmark v2** attempt, alongside the task's starting files. This directory
is the single source of truth; every pipeline reads the file from here at run
time and delivers it under its own filename (`House_Standards_v1.md`), which
is the name the prompt directive uses.

| Version | File | Introduced in |
|---|---|---|
| 1 | `House_Standards_v1.md` | gui/excel prompt versions 204/205, cli v14 then v15 (delivered as `HOUSE_STANDARDS.md`, rubric-scrubbed), coding template v11/v12 then v13 (delivered as `HOUSE_STANDARDS.md`, rubric-scrubbed, the v2 default) (2026-09-10) |

## Copies

The coding pipeline stages its own copy, `coding-agents-master/coding_agent/
prompts/house_standards_v1.md` (into the sandbox as `HOUSE_STANDARDS.md` for
templates v11/v13; v12 seeds this directory's file into `starting_files/`);
its smoke test asserts that copy is byte-identical to the file here. Every
other pipeline reads this directory directly. Edit here, then copy.

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

## Amendments

- **2026-09-18, v1 amended in place (Patrick's decision; the one exception to
  the append-only rule above).** The Rounding line changed from `"$mm, rounded
  to $0.01mm" at the head of the block` to wording that must match what the
  model does: `"shown to $0.01mm"` for display-only precision, `"rounded to"`
  only where a rounding function is applied. The version number, the
  filename and every prompt version that attaches it are unchanged, and for
  the current round of attempts the two texts count as the same standard.
  Attempts that record provenance carry the file's sha256, which tells the
  two texts apart: before `00b20f795c10bdda9823bf67578093711b52ba39191cc3052af2de3b01b67617`, after `cc4a62089d756cbb9c254a876c2eb3f6fd40864dcb33cf0592560e302f8ce342`. The coding
  pipeline's copy was updated with it (still byte-identical).

## Known interactions

- The CLI validator whitelist must include every function the standards
  recommend (`XLOOKUP`, `XMATCH`, `IFS`, `SWITCH`, `LET`); LibreOffice
  24.8+ evaluates all of them.
- The coding sandbox image (Debian bookworm) ships LibreOffice 7.4, which
  cannot evaluate `XLOOKUP`/`XMATCH`/`LET`; attempts that use them need the
  judge's `--run-calculation` (local LibreOffice 25.8) for cached values.
