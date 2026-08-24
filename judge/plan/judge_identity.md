# Plan: judge identity registry replaces prefix-sniffing model routing

## Problem

`judge/utils/llm_utils.py` decides which API endpoint a grader model hits by
sniffing the slug and consulting side channels:

| Hack | Where | What it does |
|---|---|---|
| `_openrouter_override()` | `llm_utils.py:38` | `google/*` slugs listed in env `JUDGE_OPENROUTER_MODELS` / `judge.openrouter_models` go to OpenRouter instead of Gemini-direct. Re-reads env + config on **every** `is_gemini_model` call. |
| `is_gemini_model` / `is_anthropic_model` | `llm_utils.py:49-56` | Provider = slug prefix. |
| `is_openai_model` | `llm_utils.py:59` | Provider = slug prefix **and** whether `OPENAI_API_KEY` happens to be in the env — the same slug routes differently per shell. |
| `to_api_model_id` | `llm_utils.py:68` | Strips the prefix iff the provider checks above say "direct". |
| per-provider request tweaks | `llm_utils.py:300-352`, `judge.py:2845-2860`, `judge.py:3159-3170` | `max_tokens`, drop `reasoning_effort`/`response_format`, strip images, `"none"` effort for OpenAI — all keyed on the same sniffing. |
| `openrouter_models` | `project_configs.yaml:45`, `README.md:50` | Config-side half of the override. |

Consequences: the same `--model` string can hit different endpoints depending
on env; `grade_from_db` writes only `grader_model` (the slug), so a DB row
does not record which endpoint or effort produced it; adding a model means
editing three predicates plus a config list.

## Design: `judge_identities.yaml` + `judge_identity.py`

Mirror `coding-agents-master/coding_agent/agent_identity.py` /
`agent_identities.yaml`: one registry file is THE place that says what a
grader label means. `--model <label>` looks the label up; the entry pins
every setting that changes what the judge does. Unknown label → refuse and
print a paste-ready stanza.

### Registry file — `judge/judge_identities.yaml`

```yaml
# Judge identity registry — what a grader_model label means.
# Rules (enforced at load): label unique; (provider, model, effort) unique;
# every key present on every entry.

- grader_model: google/gemini-2.5-pro          # label; stored in gradings.grader_model as-is
  provider: openrouter                          # openrouter | gemini | anthropic | openai
  model: google/gemini-2.5-pro                  # id sent on the wire
  effort: minimal                               # reasoning_effort; null = don't send

- grader_model: google/gemini-3-flash-preview
  provider: gemini
  model: gemini-3-flash-preview
  effort: minimal

- grader_model: anthropic/claude-opus-4-8
  provider: anthropic
  model: claude-opus-4-8
  effort: null

- grader_model: openai/gpt-5.5
  provider: openai
  model: gpt-5.5
  effort: none
```

Seed it with every slug that has ever been a `grader_model` value (query the
DB for `SELECT DISTINCT grader_model FROM gradings` on v1 and v2) so
re-grades under an existing label keep hitting the endpoint they used. The
current `openrouter_models` list gives the four `google/*` labels that are
OpenRouter; other `google/*` labels are `gemini`.

Label = today's slug string, unchanged, so nothing in the DB, `_MODEL_PRICING`,
`report_grading_coverage`, or paper scripts needs to move.

### Provider table — `judge/utils/judge_identity.py`

```python
PROVIDERS = {
    #  name        base_url (None = SDK default)                                  API_KEYS entry in repo_config
    "openrouter": ("https://openrouter.ai/api/v1",                                 "openrouter"),
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai/",     "gemini"),
    "anthropic":  ("https://api.anthropic.com/v1/",                                "anthropic"),
    "openai":     (None,                                                            "openai"),
}
```

`JudgeIdentity(NamedTuple)`: `grader_model, provider, model, effort`, with
`.base_url`, `.api_key_provider`, `.settings()` (dict for the DB).
`load_registry(path)`, `resolve_judge_identity(label, path)`,
`JudgeIdentityError` — same shape and refusal messages as `agent_identity.py`.

Effort lives on the identity because the provider quirks (`"none"` only on
OpenAI-direct with tools; dropped entirely on Anthropic) are model facts, not
run facts. `--reasoning-effort` stays as an explicit override for
experiments; when it differs from the pinned value the run logs a warning
and the DB records the effective value.

## Code changes

1. **`judge/utils/judge_identity.py`** — new, as above.
2. **`judge/judge_identities.yaml`** — new, seeded from the DB.
3. **`judge/utils/llm_utils.py`**
   - Delete `_openrouter_override`, `is_gemini_model`, `is_anthropic_model`,
     `is_openai_model`, `to_api_model_id`, the `_*_PREFIX` / `_*_BASE_URL`
     constants.
   - `get_client(identity: JudgeIdentity)`: cache by `identity.provider`,
     build `OpenAI(base_url=..., api_key=resolve_api_key(...))` from
     `PROVIDERS`.
   - `robust_send_message(client, messages, identity, ...)`: `kwargs["model"]
     = identity.model`; the provider-specific block becomes `if
     identity.provider == "anthropic": ...` / `== "openai"`. Replace the
     inline image-stripping copy (lines 330-352) with the existing
     `strip_unsupported_anthropic_images` — it is a verbatim duplicate.
   - Drop the `ANTHROPIC_JUDGE_DEBUG` block (dead debugging aid).
4. **`judge/main_scripts/judge.py`** — import the identity; the two agentic
   call sites (`:2845`, `:3159`) use `identity.model` / `identity.provider`.
   `JUDGE_MODEL` default comes from `judge.default_grader` (renamed from
   `openrouter_model`); resolve the identity once in `main()` and thread it
   where `model` is threaded today. Keep `model: str` = label for logging
   and cost.
5. **`grade_from_db.py`, `grade_with_orchestration.py`,
   `generate_judge_plan.py`** — resolve `args.model` → identity right after
   `parse_args`; `get_client(identity)`. `grade_from_db` adds
   `identity.settings()` to the `extra_configs`/metadata it already writes
   (`:890`, `:1224`) so a grading row records provider/model/effort.
6. **`project_configs.yaml`** — remove `openrouter_models`; rename
   `openrouter_model` → `default_grader` (one-line comment). Update
   `README.md:50` to describe the registry instead.
7. **Tests** — `judge/tests_offline/test_judge_identity.py`, mirroring
   `coding-agents-master/tests/test_agent_identity.py`: duplicate label,
   duplicate axes, missing key, unknown label prints stanza, unknown
   provider refused, `get_client` picks the right base_url per provider.

## Migration / rollout

- Env var `JUDGE_OPENROUTER_MODELS` is no longer read; grep confirms no
  shell scripts set it.
- `--model` with an unregistered slug now fails fast instead of silently
  routing to OpenRouter. This is intended: add a stanza.
- No DB schema change. Existing rows keep their `grader_model` string.
- Verify with `--nocall --dry-run` per provider, then one real attempt per
  provider (user runs these).

## Out of scope

- Pricing table (`_MODEL_PRICING`) stays keyed by label; could move into the
  registry later as `price: [in, out]`.
- cli-agents / gui-agents routing untouched.
