# Prompt registry

`registry.yaml` maps `prompt_version` to the prompt file(s) a run sends.
**One version, one prompt set.**

| Version | Turns | Prompt set |
| --- | --- | --- |
| 0 | 1 | `v000_test.txt` — pipeline smoke test, **not** a benchmark prompt |
| 9 | 1 | `prompts_pv9/SHARED_pv9_prompt.txt` — 17-check rubric |
| 200 | 3 | `prompts_v2/step1_analyze` → `step2_build` → `step3_qa` — 132-check rubric |
| 201 | 1 | `v2_1.txt` — same 132-check rubric, single pass |
| 202 | 3 | `prompts_v3/step1_analyze` → `step2_build` → `step3_qa` — 200 + Questions-sheet answers |
| 203 | 1 | `v2_2.txt` — 201 + Questions-sheet answers |
| 204 | 3 | `prompts_v4/step1_analyze` → `step2_build` → `step3_qa` — House Standards set, **rubric-free** (202 with every rubric passage removed + the house standards); attaches `../house_standards/House_Standards_v1.md` |
| 205 | 1 | `v2_3.txt` — House Standards single-pass, **rubric-free** (203 scrubbed the same way); same attachment. **Repo default.** |

Version 0 asks the agent to return the attached workbook unchanged plus one
extra sheet named `TEST SHEET` with a large bold `TEST` in A1. It exercises
upload → chat turn → download → validation → S3/DB write in seconds, and the
result is checkable by eye. Runs recorded under it are throwaway — never grade
them. A run config may write `prompt_version: 000` or `0`; YAML reads both as
the integer 0.

## Why

The version is the single key that selects the text. A `task_attempts` row
therefore tells you exactly what the agent was asked to do: `prompt_version`
alone determines what gets sent, so the DB label and the prompt cannot drift
apart.

## Attachments

An entry may also declare `attachments:` — repo-root-relative paths (`..`
allowed, so a version can reach a monorepo-level file) that are uploaded
with the task's starting files on every run of that version. 204 and 205
attach the house standards. The version selects the attachment for the same
reason it selects the text: a run config never names the file, so
`task_attempts.prompt_version` and what the agent was handed cannot drift.
`infra/run.py` resolves them once at startup (a missing file refuses the
run), appends them after each task's own files, and records their sha256
and full text in the attempt's `prompts_*.json`. An attachment file is as
immutable as the prompt text it ships with — new text = new file name + new
version (see `<monorepo>/house_standards/README.md`).

## Using it

Set `prompt_version` in the run config and nothing else:

```yaml
prompt_version: 205        # single-pass (the default); 204 for the 3-step set
```

`infra/run.py` resolves it through `infra/configs/prompt_registry.py`,
populates `prompts_file`, and writes the same number to
`task_attempts.prompt_version`. `agent.prompt_version` is derived — leave it
unset; if you do set it, it must match, and a mismatch is refused rather than
silently resolved.

There is no per-run prompt override. `prompts_file` and `prompts` are
pre-registry keys, deprecated and no longer in the config schema: a config
that still sets one loads with a deprecation warning from
`infra/configs/loader.py`, and both are slated for removal. New text goes in
`registry.yaml` under a new version.

## Adding a version

Add an entry to `registry.yaml`; **never edit an existing one**. A version
used for a real run is immutable — rows in `task_attempts` already point at
it, and rewriting the text under a live number silently invalidates that
history. New text = new number. Numbering: `9` is v1 (the number that wave
already carries), `2xx` is v2.

## Where the files live

Only new prompt sets live in this directory. The existing sets stay where
they are — `tasks_configs/prompts_v2/` and `tasks_configs/prompts_pv9/`,
both of which carry READMEs documenting their provenance — and the registry
references them by repo-relative path. They are frozen records of what
production runs have already sent, so they are not moved.
