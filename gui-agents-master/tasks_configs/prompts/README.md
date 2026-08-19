# Prompt registry

`registry.yaml` maps `prompt_version` to the prompt file(s) a run sends.
**One version, one prompt set.**

| Version | Turns | Prompt set |
| --- | --- | --- |
| 0 | 1 | `v000_test.txt` — pipeline smoke test, **not** a benchmark prompt |
| 9 | 1 | `prompts_pv9/SHARED_pv9_prompt.txt` — 17-check rubric |
| 200 | 3 | `prompts_v2/step1_analyze` → `step2_build` → `step3_qa` — 132-check rubric |
| 201 | 1 | `v2_1.txt` — same 132-check rubric, single pass |

Version 0 asks the agent to return the attached workbook unchanged plus one
extra sheet named `TEST SHEET` with a large bold `TEST` in A1. It exercises
upload → chat turn → download → validation → S3/DB write in seconds, and the
result is checkable by eye. Runs recorded under it are throwaway — never grade
them. A run config may write `prompt_version: 000` or `0`; YAML reads both as
the integer 0.

## Why

`prompt_version` used to be a label nothing checked. The run config named a
number for the DB and, separately, named `prompts_file` for the agent, so the
two could disagree — and did, leaving `task_attempts` rows that cannot tell
you what the agent was asked to do.

Now the version is the single key that selects the text: `prompt_version`
alone determines what gets sent.

## Using it

Set `prompt_version` in the run config and nothing else:

```yaml
prompt_version: 201        # single-pass; 200 for the 3-step set
```

`infra/run.py` resolves it through `infra/configs/prompt_registry.py`,
populates `prompts_file`, and writes the same number to
`task_attempts.prompt_version`. `agent.prompt_version` is derived — leave it
unset; if you do set it, it must match, and a mismatch is refused rather than
silently resolved.

`prompts_file` in a run config still wins and bypasses the registry. That is
an escape hatch for one-off experiments, and it reintroduces exactly the drift
described above: `prompt_version` becomes a label only, with nothing checking
that it describes the text being sent.

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
