# Prompt registry

`registry.yaml` maps `prompt_version` to the prompt file(s) a run sends.
**One version, one prompt set.** The repo default is **205**.

| Version | Turns | Prompt set |
| --- | --- | --- |
| 0 | 1 | `v000_test.txt` — pipeline smoke test, **not** a benchmark prompt |
| 1 | 1 | `v000_test.txt` + the House Standards attachment — smoke test for the attachment path |
| 9 | 1 | `prompts_pv9/SHARED_pv9_prompt.txt` — benchmark v1, 17-check rubric |
| 200 | 3 | `prompts_v2/step1_analyze` → `step2_build` → `step3_qa` — 132-check rubric |
| 201 | 1 | `v2_1.txt` — same rubric, single pass |
| 202 | 3 | `prompts_v3/` — 200 + Questions-sheet answers |
| 203 | 1 | `v2_2.txt` — 201 + Questions-sheet answers |
| 204 | 3 | `prompts_v4/` — House Standards set, **rubric-free**; attaches `../house_standards/House_Standards_v1.md` |
| 205 | 1 | `v2_3.txt` — House Standards single pass, **rubric-free**; same attachment. **Default.** |

Versions 0 and 1 ask the agent to return the attached workbook unchanged plus
one sheet named `TEST SHEET` with a large bold `TEST` in A1. They exercise
upload → turn → download → validation → sink in seconds. Never grade their
output. YAML reads `prompt_version: 000` and `0` as the same integer.

## Why

The version is the single key that selects the text, so
`task_attempts.prompt_version` and the prompt the agent received cannot
drift apart.

## Attachments

An entry may declare `attachments:` — repo-root-relative paths (`..` allowed)
uploaded after the task's starting files on every run of that version. 1,
204 and 205 attach the house standards. `infra/run.py` resolves them at
startup (a missing file refuses the run), appends them after each task's own
files, and records their sha256 and full text in the attempt's
`prompts_*.json`. An attachment is as immutable as the prompt text it ships
with — new text = new file name + new version (see
`<monorepo>/house_standards/README.md`).

## Using it

Set `prompt_version` in the run config and nothing else:

```yaml
prompt_version: 205        # single pass (the default); 204 for the 3-step set
```

`infra/run.py` resolves it through `infra/configs/prompt_registry.py` and
writes the same number to `task_attempts.prompt_version`.
`agent.prompt_version` is derived — leave it unset; if set, it must match.

There is no per-run prompt override. `prompts_file` and `prompts` are
deprecated pre-registry keys: a config that sets one loads with a warning from
`infra/configs/loader.py`. New text goes in `registry.yaml` under a new
version.

## Adding a version

Add an entry to `registry.yaml`; **never edit an existing one**. Rows in
`task_attempts` point at the number. Numbering: `9` is benchmark v1, `2xx` is
v2.

## Where the files live

Single-turn texts (`v000_test.txt`, `v2_1.txt`, `v2_2.txt`, `v2_3.txt`) live
in this directory. Multi-turn sets live in their own folders —
`tasks_configs/prompts_pv9/` (9), `prompts_v2/` (200), `prompts_v3/` (202),
`prompts_v4/` (204) — and the registry references them by repo-relative path.
They are frozen; never move or edit them.
