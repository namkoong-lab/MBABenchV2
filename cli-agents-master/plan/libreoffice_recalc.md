# Plan: reproducible LibreOffice recalculation for cli-agents

Status: proposal, 2026-08-23. Nothing below is implemented yet.

## The situation

After the agent writes a formula, the Excel MCP server should show it the
computed value on the next read. openpyxl can store formulas but cannot
evaluate them, so the server hands the workbook to LibreOffice for a full
recalculation:

1. `server.py` starts `LibreOfficeCalcEngine` (`excel_mcp_server/libreoffice_calc.py`),
   which spawns `soffice --headless --accept=socket,port=N`.
2. Every `set_cell_formula` / `edit_cells` runs `uno_recalc_helper.py` under a
   **separate** python (`uno_python` in `config/config.yaml`, or `$UNO_PYTHON`).
   That helper `import uno`s, connects to the socket, calls `calculateAll()`,
   and `store()`s the file with cached values written back.
3. The agent's next read sees real numbers; the saved `solution.xlsx` carries
   cached values, which is what the judge reads (`openpyxl data_only=True`).

`uno` is not a pip package. It is a C++ extension compiled inside LibreOffice,
tied to one LibreOffice build and one Python ABI. It exists only in:

- Linux: `/usr/bin/python3` after `apt-get install python3-uno`
  (pulled in by `libreoffice-calc`). This is the benchmark's production path.
- macOS: `/Applications/LibreOffice.app/Contents/Resources/python`. Nothing else.

It can never be added to the project venv, which is why the helper runs as a
subprocess in the first place.

### What breaks on macOS

The bundled LibreOffice python carries an Apple launch constraint: it may only
be spawned by LibreOffice itself. Spawned from the MCP server it wedges
(unkillable), never returning. `_start_soffice_listener` pings it 20 times with
a 5 s timeout and a 0.5 s sleep — 20 × 5.5 s = **110 s** — then raises. The MCP
client gives the server 60 s to come up, so the agent fails to start.

The current workaround (`uno_python: "/usr/bin/python3"`, the committed
default) points the probe at a python *without* `uno` so it fails in
milliseconds and the server falls through to the fallback evaluator.

### Why the fallback is not equivalent

The fallback is `_eval_formula` in `excel_mcp_server/helpers/formula_evaluation.py`
— a hand-written tokenizer/evaluator covering a subset of Excel functions.
(The log line "Falling back to _eval_formula + xlcalculator" is misleading;
`xlcalculator` is not imported anywhere in the server.) Compared with the
LibreOffice path:

| | LibreOffice (Linux) | Fallback (macOS today) |
|---|---|---|
| Scope per write | whole workbook (`calculateAll`) | the one cell just written |
| Unsupported function | correct value | `"Not available"` shown to agent |
| Saved `solution.xlsx` | cached values present | formulas only, no cached values |
| Judge without `--recalc` | grades computed values | sees `None` for every formula cell |
| Recorded in DB / batch report | no | no |

So a Mac run is a different experiment: the agent gets weaker feedback and
makes different choices, the uploaded file needs a LibreOffice pass at grading
time, and nothing in `task_attempts` says which engine produced a row. The
pv1105 cohort was run this way; its numbers are fallback-engine numbers.

## Goal

Pick **one** of the two paths below so that a benchmark run started from a
laptop is reproducible against Linux cohorts. Either is acceptable; they are
not mutually exclusive long-term, but only one should be built first.

### Option A — run cli-agents on AWS boxes, like gui-agents

Reuse `gui-agents-master/infra/dispatcher/` (`spinup` / `assign` / `status` /
`teardown`, `boxes.yaml`, `helper/provision.py`). The laptop becomes a
dispatcher; every task executes on Ubuntu where `python3-uno` is the normal
path and the question of macOS never arises.

What has to change:

1. **Provisioning.** `helper/provision.py` installs Chrome via `USER_DATA`.
   Add a cli-agents profile that `apt-get install -y libreoffice-calc
   python3-uno` instead, installs the repo + `uv` venv, and pushes
   `config/config.yaml` (DB URLs, AWS keys, `keys.anthropic_api_key`) the same
   way `_write_secrets_env` does today.
2. **Worker loop.** gui-agents boxes poll the DB for eligible tasks
   (`_list_eligible_tasks`, `cmd_assign`). cli-agents already has
   `auto_batch_runner.py` which claims tasks from `task_attempts`; wire it as
   the on-box service so `assign` can point boxes at a batch config.
3. **Config templates.** One `config_templates/cli_<model>.yaml` per agent,
   mirroring `examples/v2/*.yaml`, so `spinup --config-template` works
   unchanged.
4. **Concurrency.** cli-agents caps at 4 processes per machine (one `soffice`
   each, see `docs/ARCHITECTURE.md` Deployment Notes). `t3.large` (8 GiB)
   for 4 workers; `t3.medium` is too small — gui-agents already found that
   Chrome OOMs it.
5. **Provenance.** Add `recalc_engine` (+ LibreOffice version) to the
   `task_attempts` metadata and the batch report so a fallback run can never
   again be mistaken for a LibreOffice run.

Effort: ~2–3 days, most of it in provisioning and the worker loop. Pays off
only if we keep running large cohorts; also removes the laptop as a
bottleneck and the 4-process ceiling.

### Option B — make LibreOffice recalc work locally, by pinning its location

Keep running on the laptop but require a real LibreOffice and make the engine
use it correctly. Two sub-approaches, in order of preference:

**B1. Drive `soffice` directly, drop the `uno` python entirely.**
`soffice --headless --convert-to xlsx --outdir <tmp> <file>` performs a full
recalc and writes cached values, with no UNO socket and no second interpreter.
Cost is a process launch per recalc (~1–2 s on a warm profile) versus ~200 ms
over the socket. Changes:

- Replace `uno_recalc_helper.py --recalc` with a `subprocess.run([soffice_path,
  "--headless", "--convert-to", "xlsx", ...])` in `LibreOfficeCalcEngine.recalculate`.
- Add `libreoffice_path` to `config/config_default.yaml` (default: `soffice` on
  PATH; macOS users pin `/Applications/LibreOffice.app/Contents/MacOS/soffice`).
  `uno_python` goes away.
- Keep a persistent `-env:UserInstallation` profile dir so every launch is
  warm.
- Verify that `--convert-to xlsx` round-trips formatting/named ranges the
  judge relies on (it should; it is the same filter `store()` uses).

This removes the platform-specific python problem outright. Same binary,
same filter, on Mac and Linux.

**B2. Keep UNO, fix the macOS spawn.** Strip the launch constraint from the
bundled interpreter once (`codesign --force --sign - .../Resources/python`
and the framework it execs), then pin `uno_python` to it. Works, but is a
per-machine hack that breaks on every LibreOffice update and needs
documenting for each new Mac. Only worth it if B1's per-call latency turns
out to matter.

Either way, Option B also needs:

- **Fail loudly.** When `libreoffice_path` is set but the engine cannot start,
  refuse to run the batch (or require an explicit `--allow-fallback`) instead
  of silently degrading. Today the degradation is one stderr line.
- **Provenance**, same as A5.
- **Judge parity.** Run the judge's `--recalc` on Linux CI regardless, so a
  file from any engine is graded from the same LibreOffice pass.

Effort: B1 ~1 day including verification that outputs are byte-equivalent
for the judge's purposes; B2 ~half a day but fragile.

## Recommendation

Do **B1** now: it is the smaller change, it fixes the root cause (a second
interpreter that only LibreOffice can host) rather than routing around it,
and it makes laptop runs genuinely comparable to Linux ones. Revisit **A**
when cohort size makes the laptop the bottleneck; at that point B1's
`libreoffice_path` config carries over unchanged to the boxes.

## Verification

Regardless of option, the acceptance test is:

1. Run the same task with the same model + prompt version on (a) an Ubuntu
   box with `python3-uno` and (b) the target environment.
2. Diff the tool-call transcripts: every `set_cell_formula` response should
   carry `"engine": "libreoffice"` and identical `calculated_value`.
3. `openpyxl.load_workbook(solution.xlsx, data_only=True)` returns non-`None`
   for every formula cell in both files without a separate recalc step.
4. The `task_attempts` row records the engine and LibreOffice version.

## Immediate housekeeping (independent of the decision)

- Correct the "xlcalculator" wording in `server.py:63` and docs to say
  `_eval_formula`.
- Add a macOS line to `README.md`'s setup block next to the `apt-get` one.
- Log a warning at engine start naming the fallback and pointing at this file.
