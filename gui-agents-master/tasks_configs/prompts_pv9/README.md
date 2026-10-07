# Benchmark v1 prompt (prompt_version 9)

The single-turn payload for benchmark v1 (`benchmark: v1`, the BizbenchV1
task set), reached through `prompt_version: 9` in
`tasks_configs/prompts/registry.yaml`. Every agent is sent exactly one
prompt per task.

| File | Role |
|---|---|
| `SHARED_pv9_prompt.txt` | The file the registry sends: `SHARED_rubric_preamble.txt` + `AGENT_closing.txt` |
| `SHARED_rubric_preamble.txt` | The 17-check rubric preamble on its own |
| `AGENT_closing.txt`, `PRO_closing.txt` | The two closings (three steps vs one uninterrupted pass) |
| `claude_web_FULL.txt`, `chatgpt_agent_FULL.txt` | Byte-identical to `SHARED_pv9_prompt.txt`; kept for reference |
| `chatgpt_pro_FULL.txt` | Preamble + `PRO_closing.txt`; reference only, not registered |

Not to be confused with the `2xx` sets, which are benchmark v2 text against a
different (132-check) rubric. These files are frozen; new text = new version.
