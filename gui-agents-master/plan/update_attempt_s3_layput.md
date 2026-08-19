# Backfill existing v2 attempts into the per-attempt S3 layout

**Status:** proposed — nothing executed yet.
**Last updated:** 2026-08-19
**Owner:** whoever runs the migration; it touches production S3 + the MBABenchV2 DB.

## Context

`MBABenchV2PostgresS3AttemptSink` now files every artifact of one attempt into its own folder
(`task_io/sinks/postgres_s3.py:526-535`):

```
{s3_prefix}/{agent_folder}/{task_name}/{timestamp}_{run_id}/{filename}
```

The previous layout dropped every attempt for a task into one flat folder:

```
{s3_prefix}/{agent_folder}/{task_name}/{filename}
```

and relied on the engine's per-file timestamps to tell attempts apart. Attempts written before the
change therefore stay flat, so `s3://mbabench/MBABenchV2/attempts/` now holds two shapes. Nothing
breaks — every consumer treats the URI as opaque (see **Invariants**) — but "download this
attempt" stays a filename-parsing exercise for the old rows, and the two shapes make any future
prefix-level tooling (lifecycle rules, per-attempt deletion, sync-to-local) special-case one of
them forever.

This plan converges the old rows onto the new layout: move the objects, rewrite the URIs in
`task_attempts`, and leave the bucket with exactly one shape under the v2 GUI subtree.

## Scope

**In scope.** `task_attempts` rows in the **MBABenchV2** DB with `agent_model_type = 'gui'`, and
the S3 objects their `attempt_files` / `prompt_files` reference.

**Out of scope.**

- **BizbenchV1 / benchmark v1.** `BizbenchPostgresS3AttemptSink` keeps its Hive layout
  (`postgres_s3.py:479-497`) — unchanged by the code change, so there is nothing to converge.
- **CLI- and coding-agent rows.** They write into the *same* `task_attempts` table and can share
  the `MBABenchV2/attempts/` prefix (`cli-agents-master/excel_cli_agent/auto_batch_runner.py:83-90`,
  with `_s3_root` from the benchmark config at line 150), but under their own layout and their own
  `agent_model_type`: `'api'` for cli-agents (`auto_batch_runner.py:623`) and `'coding_cli'` for
  coding-agents (`coding-agents-master/coding_agent/recorder.py:115`). **`agent_model_type = 'gui'`
  is the scope filter** — a prefix-only filter would sweep their objects too.
- **Deleting orphans.** Objects referenced by no row are reported, not touched (see Phase 7).
  Deleting the *superseded* copies of migrated objects is in scope — that is Phase 6.
- **The local `outputs/` mirror.** New runs mirror automatically; backfilling it is optional
  (Phase 8).

## Invariants the migration must preserve

1. **Basenames never change.** Consumers derive the display name with `Path(uri).name`
   (`judge/main_scripts/grade_from_db.py:391-413`, `judge/operation_scripts/cache_solution_csvs.py:83`).
   Only the key prefix moves.
2. **File count per row never changes.** `check_attempt_completion.py:117-119` requires exactly one
   `.xlsx` in `attempt_files`; the judge fails an attempt with none.
3. **No row is ever left pointing at a nonexistent object.** This is what fixes the ordering in
   Phases 3-6: copy → rewrite the DB → verify every reference resolves → only then delete.
4. **`deprecated` rows migrate too.** They are excluded from grading
   (`grade_from_db.py:308`) but are still evidence, and leaving them flat would defeat the point of
   converging the prefix.

## Mapping rule

For attempt row `A` with joined `tasks` row `T`, each referenced object moves to:

```
{everything up to and including the agent_folder segment of the OLD key}
  / {sanitize(T.task_name)}
  / {end_ts}_a{A.id}
  / {basename of the OLD key}
```

- **The old key supplies the prefix and `agent_folder`.** Do not reconstruct them from
  `agent_identity.py` — the object is already filed under a real folder, and reusing it makes the
  migration independent of identity-table history. Cross-check only: `agent_model_name` equals
  `agent_folder` for every identity in every table (verified across all four tables in
  `infra/configs/agent_identity.py`), so a segment that disagrees with `A.agent_model_name` means
  the key is not what we think it is → **skip the row and report it**.
- **`sanitize` is `_sanitize_s3_segment`** (`postgres_s3.py:53-63`) — import it, do not
  reimplement.
- **The task folder is rebuilt from the current `T.task_name`, not the old key.** A task renamed
  since the attempt ran is silently healed by this; the old objects are still located by exact URI
  string, so a rename cannot cause a miss.
- **`end_ts` = `A.end_time` as `%Y%m%d_%H%M%S`.** The live sink stamps publish time
  (`postgres_s3.py:311`), which is `end_time` plus the upload; `end_time` is the honest analog.
  Fall back to `start_time`, then to the timestamp embedded in the filename, and report which was
  used.
- **`a{A.id}` stands in for `run_id`.** Historical attempts have no run uuid, and inventing an
  8-hex one would fabricate provenance. The attempt's primary key is unique, deterministic (so the
  migration is idempotent and resumable), and joins the folder straight back to its row. The
  `a` prefix makes backfilled folders greppable and visibly distinct from `{ts}_{8 hex}` folders
  written by a live run.

## Phase 0 — Inventory (read-only; run this first)

Nothing below is designed until these numbers are known. Run against the **v2** DB.

```sql
-- How much is in scope, and over what period?
SELECT count(*) AS rows,
       count(*) FILTER (WHERE deprecated) AS deprecated_rows,
       count(*) FILTER (WHERE agent_failed) AS failed_rows,
       min(start_time), max(start_time)
FROM task_attempts
WHERE agent_model_type = 'gui';

-- Which key shapes actually exist? (drives whether the mapping rule covers everything)
WITH uris AS (
  SELECT id, jsonb_array_elements_text(attempt_files::jsonb) AS uri
    FROM task_attempts WHERE agent_model_type = 'gui'
  UNION ALL
  SELECT id, jsonb_array_elements_text(prompt_files::jsonb)
    FROM task_attempts WHERE agent_model_type = 'gui'
)
SELECT CASE
         WHEN uri ~ '/[0-9]{8}_[0-9]{6}_[0-9a-f]{8}/[^/]+$' THEN 'new (live run)'
         WHEN uri ~ '/[0-9]{8}_[0-9]{6}_a[0-9]+/[^/]+$'     THEN 'new (backfilled)'
         WHEN uri ~ '/task_id=[0-9]+/'                      THEN 'legacy base-class shape'
         WHEN uri LIKE 's3://mbabench/MBABenchV2/attempts/%' THEN 'old flat'
         ELSE 'UNEXPECTED'
       END AS shape,
       count(*), count(DISTINCT id) AS attempts
FROM uris GROUP BY 1 ORDER BY 2 DESC;

-- Objects referenced by more than one attempt (must be copied to both, deleted after neither needs it)
WITH uris AS (
  SELECT id, jsonb_array_elements_text(attempt_files::jsonb) AS uri
    FROM task_attempts WHERE agent_model_type = 'gui'
  UNION ALL
  SELECT id, jsonb_array_elements_text(prompt_files::jsonb)
    FROM task_attempts WHERE agent_model_type = 'gui'
)
SELECT uri, count(DISTINCT id) FROM uris GROUP BY 1 HAVING count(DISTINCT id) > 1;

-- Rows that would collide on the target folder (should return zero: id is unique)
-- and rows with no end_time (fallback path exercised)
SELECT count(*) FROM task_attempts
WHERE agent_model_type = 'gui' AND end_time IS NULL;
```

Also, from the shell:

```bash
aws s3 ls --recursive s3://mbabench/MBABenchV2/attempts/ | wc -l      # total objects
aws s3api get-bucket-versioning --bucket mbabench                     # is delete reversible?
```

**The versioning answer sets how much rides on Phase 5.** With versioning on, the Phase 6 deletes
are recoverable by version id; with it off, they are permanent and Phase 5 is the only thing
standing between a mapping bug and lost attempt files.

Record the counts in this file before proceeding.

## Phase 1 — Manifest (dry run, no writes)

Add `gui-agents-master/infra/migrations/migrate_v2_attempt_layout.py`:

```
--dry-run          default; writes the manifest and exits
--apply            Phases 3-4: perform copies + DB updates (manifest already reviewed)
--verify-only      Phase 5: HEAD every URI the DB now holds; read-only, re-runnable
--delete-old       Phase 6: separate invocation, never implied by --apply
--attempt-ids …    restrict to specific rows (pilot runs)
--manifest PATH    default infra/migrations/manifests/{ts}_v2_layout.json
```

The manifest is one entry per **object**: `attempt_id`, `column` (attempt_files / prompt_files),
`old_uri`, `new_uri`, `decision` (`move` / `already-new` / `skip:<reason>`). It is the review
artifact, the resume log, and the rollback script — everything after this phase reads it rather
than re-deriving from the DB.

Refuse to emit a manifest and exit non-zero if any row hits: an unexpected key shape, an
`agent_folder` segment that disagrees with `agent_model_name`, an object that HEADs as missing, or
a target key that already exists with a different size. Those are inventory surprises, not
per-object errors to skip past silently.

**Review the manifest by hand** — at minimum a per-`decision` count, and a spot-check of a few
`new_uri`s against the Phase 0 shape query.

## Phase 2 — Freeze

Migration is not safe against concurrent writers: a run finishing mid-migration inserts a row the
manifest does not know about, and a grading run resolves URIs that Phase 4 is rewriting.

- No `infra.run` / worker loop active (`infra/worker/worker_loop.py`), on any box.
- No judge run active (`judge/main_scripts/grade_from_db.py`).

Rows created after the manifest was built are simply left for a second pass — the migration is
idempotent, so re-running picks them up. The freeze is about not rewriting URIs under a reader's
feet.

## Phase 3 — Copy

Server-side `copy_object` (`CopySource` → new key) per manifest entry. No download, no re-upload;
every artifact here is far under the 5 GB single-copy limit. Then `head_object` the destination
and compare `ContentLength` (and `ETag`, valid to compare for these single-part copies) against the
source before marking the entry copied.

Both keys exist after this phase, and stay that way until Phase 6. That overlap is the whole
safety margin: it is what lets Phase 4 rewrite URIs without a window where a row points nowhere,
and what makes Phase 5's verification meaningful — nothing is destroyed until the new keys have
been proven good.

## Phase 4 — Rewrite the DB

Per attempt row, in **one transaction per row**, and only once every object for that row is marked
copied:

```sql
UPDATE task_attempts
   SET attempt_files = %s::json, prompt_files = %s::json
 WHERE id = %s;
```

Write the full new arrays, preserving element order (the judge takes the first `.xlsx` it finds;
reordering is a behavior change for rows with more than one). Guard with a re-read of the row
inside the transaction — if `attempt_files` no longer matches the manifest's `old_uri` list, some
other writer touched it: skip and report.

Mark each row done in the manifest as it commits, so an interrupted run resumes exactly where it
stopped.

## Phase 5 — Verify every DB reference resolves (hard gate on Phase 6)

The deletion in Phase 6 is irreversible without bucket versioning, so nothing is deleted until
every rewritten row has been *proven* to point at a real object.

Read the rows back **from the database, not from the manifest** — the manifest records what the
migration believes it wrote; the DB is what the judge will actually read. For every in-scope row
(including `deprecated` ones), for every URI in `attempt_files` and `prompt_files`:

1. **The object exists.** `head_object` on each URI. This is the check the phase is named for and
   the one that must be 100% clean — ~3 objects per row, HEAD only, no downloads.
2. **The bytes match.** `ContentLength` equals the size the manifest recorded for the source
   object, catching a truncated or half-written copy that still HEADs 200.
3. **The invariants hold** (see above): basename unchanged from `old_uri`; URI count per column
   unchanged; still exactly one `.xlsx` in `attempt_files`; every URI matches the new-shape regex
   from Phase 0; and all of a row's files share one folder — which is the entire point of the
   layout and the one property the old data could not have.
4. **`judge/operation_scripts/check_attempt_completion.py`** over the migrated task set returns
   the same verdicts as the pre-migration baseline. Capture that baseline **before Phase 3**.
5. **One real judge invocation** on a migrated attempt, confirming the download path resolves
   end to end — the only check that exercises `extract_file_refs` → S3 fetch for real.

Expose this as `--verify-only` so it can be re-run at will: it is also a standing integrity check
over the GUI subtree, independent of any migration.

**Any failure stops the migration — it does not skip a row.** A row that fails here means the
mapping or copy logic is wrong in a way that probably affects more rows than the one that
surfaced it. Fix, re-run Phase 3/4 for the affected rows, then re-verify. Phase 6 runs only from a
completely clean report.

## Phase 6 — Delete the old copies (`--delete-old`, separate invocation)

Preconditions, all hard: Phase 4 committed for every row, Phase 5 clean with zero failures, and
the manifest stored somewhere durable (it is the only rollback path).

Per manifest entry:

1. `head_object` the new key one more time — the delete and its justification should not be
   separated by a stale read.
2. Confirm the old key is referenced by **no** current `task_attempts` row. Query across all
   rows, not just the in-scope ones: it is one cheap query and it is what protects a shared object
   (the Phase 0 duplicate-reference query flags these up front).
3. `delete_object`. Batch with `delete_objects` (1000 keys/request) for throughput, but keep the
   per-key preconditions above — the batch API reports per-key results, so a partial failure is
   visible and resumable.

If the Phase 0 versioning check said versioning is **on**, these deletes are recoverable by version
id and the risk is low. If it said **off**, Phase 5 and the manifest are the entire safety net,
which is exactly why Phase 5 is a gate rather than a report.

## Phase 7 — Reconcile

1. **Re-run the Phase 0 shape query.** Expect only `new (live run)` and `new (backfilled)`; a
   remaining `old flat` row means Phase 4 missed it.
2. **Orphan report.** `aws s3 ls --recursive` the GUI subtree, subtract every URI referenced by
   any `task_attempts` row. What remains should be only: files from runs that died before
   `sink.publish()`, and old flat-layout files that were silently overwritten when two attempts
   for one task landed in the same second (unrecoverable — the old layout had no guard against
   it; the new one does). Report them, delete nothing: unlike the Phase 6 deletes, these have no
   verified replacement standing behind them.

## Phase 8 — Optional: hydrate `outputs/` for historical attempts

The local mirror (`postgres_s3.py:222-242`) only covers runs from the change onward. To give old
attempts the same on-disk presence:

```bash
aws s3 sync s3://mbabench/MBABenchV2/attempts/ \
            gui-agents-master/outputs/MBABenchV2/attempts/
```

Post-migration this reproduces the mirror layout exactly, since the mirror path *is* the key path.
`outputs/` is gitignored (`gui-agents-master/.gitignore:74`). Skip if disk is a concern — this is
convenience, not preservation.

## Rollback

- **Before Phase 4:** nothing to roll back. Delete the copied keys listed in the manifest.
- **After Phase 4, before Phase 6** (including anything Phase 5 catches): replay the manifest in
  reverse (`new_uri` → `old_uri` per row). Both objects still exist, so this is a DB-only revert —
  which is the reason the delete sits behind the verification rather than beside the copy.
- **After Phase 6:** re-copy `new_uri` → `old_uri` from the manifest, then revert the DB. Possible
  because the manifest records both keys for every object — which is why it must be committed
  somewhere durable (not just `/tmp`) before Phase 3 starts. If bucket versioning is on, restoring
  the deleted version is the faster path.

## Follow-ups worth landing with this

- **A regression test for the layout.** An offline test asserting one folder per attempt, mirror
  set == S3 key set, and the v1 layout unchanged (stub `_s3`/`_insert_row`, no AWS/DB). This was
  written ad hoc to validate the sink change; it belongs in `tests/` as
  `test_sink_layout_offline.py` so the layout cannot regress silently.
- **Document the `a{id}` convention** in the `MBABenchV2PostgresS3AttemptSink` docstring, so a
  reader who finds a `_a1487` folder in the bucket knows it is a backfill, not a malformed run id.

## Open questions

1. **How long to soak between Phase 5 and Phase 6?** Deleting the old copies is decided; deleting
   them *immediately* after verification is not. A few days between the two invocations costs a
   few MB and lets any unnoticed consumer surface while the rollback is still DB-only.
2. **Do rows with the legacy `task_id=` shape exist in v2?** Phase 0 answers it. If yes, they need
   their own mapping (no `agent_folder` in the key), likely reconstructed from `agent_model_name`.
3. **Is a pilot wanted?** Recommended: `--attempt-ids` over one task's attempts, all the way
   through Phase 6, before the full sweep.
