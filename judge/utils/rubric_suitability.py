"""Per-task rubric suitability gating (Phase A of the 2026-08 judge update).

Julian's per-task annotations (s3://<bucket>/MBABenchV2/rubric_suitability/)
mark each of the 132 rubric_9 checks applicable or not_applicable for that
task. The agentic judge skips not_applicable checks entirely — never prompted,
never scored — and renormalizes within the category (emergent: the scorer
divides by the summed weight of the checks it is given). CategoryWeights are
untouched unless a category loses every check (defensive; no task does today).

Selection rule: the latest complete annotation by annotator "julian" —
top-level files only (each task folder also carries a history/ subfolder of
earlier saves; those never participate in selection). `conditional` flags are
recorded for audit, ignored for scoring. The annotator tool's rubric_version
hash is stored as provenance only — validation is by (no, category, name)
match against the loaded rubric, which since the 2026-08 in-place rubric_9
revision is exact 132/132.

Enforcement (wired in judge.agentic_judge_case): a v2 grading without a
staged annotation refuses to run; JUDGE_SKIP_SUITABILITY=1 grades ungated and
records that in scored_results.rubric_suitability.
"""

import copy
import json
import os
from pathlib import Path

try:
    from .logger import logger
except ImportError:  # imported as a bare module (utils/ on sys.path)
    from logger import logger

S3_PREFIX_TEMPLATE = "{s3_root}/rubric_suitability/task_id={task_id}/"
ANNOTATOR = "julian"
STAGED_FILENAME = "rubric_suitability.json"
SKIP_ENV = "JUDGE_SKIP_SUITABILITY"


class SuitabilityError(Exception):
    """A v2 grading cannot proceed without a valid suitability annotation."""


# --------------------------------------------------------------------------
# S3 fetch + pin (callers own the client; used by grade_from_db)
# --------------------------------------------------------------------------


def select_annotation_key(keys_with_meta: list[dict]) -> dict:
    """Pick the annotation to use from listed candidates.

    `keys_with_meta`: [{"key": s3_key, "annotator": str, "complete": bool,
    "created_at": str}, ...] — already restricted to top-level files.
    Latest complete julian wins; created_at ties break on the key string so
    the choice is deterministic.
    """
    candidates = [
        m for m in keys_with_meta
        if m.get("annotator") == ANNOTATOR and m.get("complete") is True
    ]
    if not candidates:
        raise SuitabilityError(
            f"no complete '{ANNOTATOR}' annotation among {len(keys_with_meta)} "
            f"candidate file(s)"
        )
    return max(candidates, key=lambda m: (m.get("created_at") or "", m["key"]))


def fetch_annotation(s3_client, bucket: str, s3_root: str, task_id: int,
                     cache_dir: Path) -> tuple[dict, str]:
    """Fetch the selected annotation for a task; returns (annotation, s3_key).

    Lists only the task folder's top level (Delimiter='/') so history/ saves
    are excluded, reads every candidate's header fields, applies the
    selection rule, and caches the chosen file under cache_dir by basename.
    """
    prefix = S3_PREFIX_TEMPLATE.format(s3_root=s3_root, task_id=task_id)
    resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    keys = [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".json")]
    if not keys:
        raise SuitabilityError(f"no suitability annotations at s3://{bucket}/{prefix}")

    # Per-task subdirectory: basenames are annotator_timestamp and two tasks
    # annotated in the same second would otherwise collide in the cache.
    cache_dir = Path(cache_dir) / f"task_id={task_id}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def _read(key: str) -> dict:
        local = cache_dir / Path(key).name
        if local.exists():
            return json.loads(local.read_text())
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        local.write_bytes(body)
        return json.loads(body)

    metas = []
    docs = {}
    for key in keys:
        doc = _read(key)
        docs[key] = doc
        metas.append({
            "key": key,
            "annotator": doc.get("annotator"),
            "complete": doc.get("complete"),
            "created_at": doc.get("created_at"),
        })
    chosen = select_annotation_key(metas)
    return docs[chosen["key"]], chosen["key"]


# --------------------------------------------------------------------------
# Validation + filter construction (pure; used by judge.py and tests)
# --------------------------------------------------------------------------


def validate_annotation(annotation: dict, rubric: dict) -> None:
    """Require an exact (no, category, name) match against the loaded rubric.

    The rubric's flattened order (category key order, then within-category
    order) must equal the annotation's `no` 1..N sequence. Refuses on any
    drift — do not grade against a rubric edition the annotation didn't see.
    """
    entries = annotation.get("rubrics")
    if not isinstance(entries, list) or not entries:
        raise SuitabilityError("annotation has no 'rubrics' entries")

    flat = [(cat, c["name"]) for cat, checks in rubric.items() for c in checks]
    if len(entries) != len(flat):
        raise SuitabilityError(
            f"annotation covers {len(entries)} checks, rubric has {len(flat)}"
        )
    mismatches = []
    for e in entries:
        no = e.get("no")
        if not isinstance(no, int) or not (1 <= no <= len(flat)):
            mismatches.append(f"bad 'no': {no!r}")
            continue
        expected = flat[no - 1]
        if (e.get("category"), e.get("name")) != expected:
            mismatches.append(
                f"no={no}: annotation ({e.get('category')!r}, {e.get('name')!r}) "
                f"!= rubric {expected!r}"
            )
    if mismatches:
        raise SuitabilityError(
            "annotation does not match the loaded rubric:\n  "
            + "\n  ".join(mismatches[:10])
            + (f"\n  ... {len(mismatches) - 10} more" if len(mismatches) > 10 else "")
        )
    verdicts = {e.get("verdict") for e in entries}
    unknown = verdicts - {"applicable", "not_applicable"}
    if unknown:
        raise SuitabilityError(f"unknown verdict value(s): {sorted(unknown)}")


def build_suitability(annotation: dict, rubric: dict, s3_key: str = None) -> dict:
    """Validated annotation -> filter + provenance.

    Returns {
      "applicable": {category: [names]},   # in rubric order
      "excluded":   {category: [names]},   # in rubric order
      "provenance": {... for scored_results / _metadata.json ...},
    }
    """
    validate_annotation(annotation, rubric)
    by_no = {e["no"]: e for e in annotation["rubrics"]}
    applicable: dict[str, list] = {}
    excluded: dict[str, list] = {}
    no = 0
    for cat, checks in rubric.items():
        applicable[cat] = []
        excluded[cat] = []
        for c in checks:
            no += 1
            entry = by_no[no]
            if entry["verdict"] == "applicable":
                applicable[cat].append(c["name"])
            else:
                excluded[cat].append(c["name"])
    n_excluded = sum(len(v) for v in excluded.values())
    provenance = {
        "s3_key": s3_key,
        "annotator": annotation.get("annotator"),
        "created_at": annotation.get("created_at"),
        "rubric_version": annotation.get("rubric_version"),
        "applicable_count": sum(len(v) for v in applicable.values()),
        "excluded_count": n_excluded,
        "excluded": {cat: names for cat, names in excluded.items() if names},
        "conditional_flags": sorted(
            e["no"] for e in annotation["rubrics"] if e.get("conditional")
        ),
    }
    return {"applicable": applicable, "excluded": excluded, "provenance": provenance}


def build_effective_weights(weights_data: dict, excluded: dict) -> dict:
    """Deep-copy the weights and drop excluded checks from each category.

    Renormalization within a category is emergent — calculate_scores divides
    by the summed weight of the checks present. If a category loses every
    check (cannot happen with today's annotations), it is dropped from both
    the per-category lists and CategoryWeights, and CategoryWeights is
    renormalized over the remainder — logged loudly.
    """
    eff = copy.deepcopy(weights_data)
    emptied = []
    for cat, names in excluded.items():
        if not names or cat not in eff:
            continue
        drop = set(names)
        eff[cat] = [c for c in eff[cat] if c["name"] not in drop]
        if not eff[cat]:
            emptied.append(cat)
    if emptied:
        logger.warning(
            f"  SUITABILITY: categories with ZERO applicable checks: {emptied} — "
            f"dropping them and renormalizing CategoryWeights (defensive path; "
            f"no known annotation does this)"
        )
        cw = eff["CategoryWeights"][0]
        for cat in emptied:
            cw.pop(cat, None)
            eff.pop(cat, None)
        total = sum(cw.values())
        if total > 0:
            eff["CategoryWeights"] = [{c: w / total for c, w in cw.items()}]
    return eff


# --------------------------------------------------------------------------
# Case-level loader (judge.py entry point)
# --------------------------------------------------------------------------


def skip_requested() -> bool:
    return os.environ.get(SKIP_ENV) == "1"


def load_for_case(task_path: Path, rubric: dict, benchmark: str | None) -> dict | None:
    """Load the staged annotation for a task folder, enforcing the v2 rule.

    Returns the build_suitability() dict, or None to grade ungated (v1 /
    unknown benchmark, or the JUDGE_SKIP_SUITABILITY=1 escape hatch — the
    caller records the skip in scored_results).

    Raises SuitabilityError for a v2 grading with a missing or invalid
    annotation and no escape hatch.
    """
    staged = Path(task_path) / STAGED_FILENAME
    if skip_requested():
        logger.warning(
            f"  SUITABILITY: {SKIP_ENV}=1 — grading UNGATED (all 132 checks); "
            f"recorded in scored_results"
        )
        return None
    if not staged.exists():
        if benchmark == "v2":
            raise SuitabilityError(
                f"v2 grading requires a rubric suitability annotation, but "
                f"{staged} is missing (grade_from_db stages it; for standalone "
                f"judge runs place the annotation JSON there). Set {SKIP_ENV}=1 "
                f"to grade ungated."
            )
        return None
    annotation = json.loads(staged.read_text())
    if annotation.get("complete") is not True:
        raise SuitabilityError(f"staged annotation {staged} is not complete")
    meta = annotation.get("_staging", {})
    return build_suitability(annotation, rubric, s3_key=meta.get("s3_key"))
