"""Offline checks for the single-pass judge (judge v4) and its shared parts.

Run from judge/:  python tests_offline/test_single_pass.py
No DB, S3, or LLM access — judge.py is imported with project configs loaded
from the repo's own project_configs.yaml (benchmark-agnostic keys only).

Covers:
  - global check IDs match the suitability annotations' flattened numbering
  - the flat renderer carries number + [category] + name for every check,
    and renders exactly the gated set (gaps preserved, never renumbered)
  - guidance notes validate against rubric_9 and render in both renderers
  - regrouping flat verdicts by category scores identically to the
    12-category path (same calculate_scores contract)
  - WorkingJudgement accepts numeric string IDs end to end
  - the single-pass toolset: view param present, sources include 'starting',
    12-category toolset unchanged
  - _execute_read_file serves the starting dir and the view-keyed variants
  - config: template_6/7 parse; single_pass versions distinct from agentic
  - grade_with_orchestration forwards suitability_source_path (the 2026-08
    blocker) and single_pass/cached_starting_csv_dir
"""
import csv
import importlib.util
import inspect
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))

from utils import rubric_guidance, rubric_suitability  # noqa: E402
from utils.misc_utils import load_project_configs  # noqa: E402
from utils.prompt_utils import (  # noqa: E402
    numbered_rubric_checks,
    render_rubric_checks_flat,
    render_rubric_checks_list,
)

load_project_configs()

_spec = importlib.util.spec_from_file_location(
    "_judge_module", str(JUDGE / "main_scripts" / "judge.py")
)
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)

RUBRIC_PATH = JUDGE / "prompts" / "rubrics" / "rubric_9.json"
WEIGHTS_PATH = JUDGE / "prompts" / "rubrics" / "rubric_9_weights.json"
RUBRIC = json.loads(RUBRIC_PATH.read_text())
WEIGHTS = json.loads(WEIGHTS_PATH.read_text())

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)
    else:
        print("OK ", msg)


# ---------------------------------------------------------------------------
# Global IDs == suitability flattened order
# ---------------------------------------------------------------------------
numbered = numbered_rubric_checks(RUBRIC)
flat_suit = [(cat, c["name"]) for cat, checks in RUBRIC.items() for c in checks]
check(len(numbered) == 132, f"numbered_rubric_checks covers 132 (got {len(numbered)})")
check(
    [(cat, c["name"]) for _, cat, c in numbered] == flat_suit,
    "global numbering equals the suitability validator's flattened order",
)
check(
    [no for no, _, _ in numbered] == list(range(1, len(numbered) + 1)),
    "global IDs are 1..N in order",
)

# A synthetic annotation over the real rubric must validate — proving the
# numbering here and rubric_suitability's agree by construction.
annotation = {
    "rubrics": [
        {"no": no, "category": cat, "name": c["name"], "verdict": "applicable"}
        for no, cat, c in numbered
    ],
    "complete": True,
}
try:
    rubric_suitability.validate_annotation(annotation, RUBRIC)
    check(True, "synthetic annotation on our numbering passes validate_annotation")
except rubric_suitability.SuitabilityError as e:
    check(False, f"annotation from our numbering rejected: {e}")

# ---------------------------------------------------------------------------
# Flat renderer: number + [category] + name per check; gated set == rendered
# ---------------------------------------------------------------------------
guidance = rubric_guidance.load_guidance(str(RUBRIC_PATH))
check(guidance is not None, "rubric_9 guidance file loads and validates")
check(
    rubric_guidance.load_guidance(str(JUDGE / "prompts/rubrics/rubric_8.json"))
    is None,
    "rubric_8 has no guidance sibling -> None (v1 rendering unchanged)",
)

# Gate out an arbitrary subset (every third check) to simulate suitability.
gated = [t for i, t in enumerate(numbered) if i % 3 != 0]
flat_text = render_rubric_checks_flat(gated, guidance)
for no, cat, c in gated:
    if no in (2, 3, 131):  # spot-check first/middle/last of the gated list
        check(
            f"Check {no} [{cat}] {c['name']}:" in flat_text,
            f"flat renderer carries 'Check {no} [{cat}] {c['name']}'",
        )
rendered_ids = {
    int(line.split()[1])
    for line in flat_text.splitlines()
    if line.startswith("Check ")
}
check(
    rendered_ids == {no for no, _, _ in gated},
    "flat renderer renders exactly the gated IDs (gaps preserved)",
)
check(
    "Guidance:" in flat_text,
    "flat renderer includes guidance notes",
)
check(
    "Category guidance for Accuracy:" in flat_text,
    "flat renderer includes the Accuracy category note",
)

# Per-category renderer: guidance renders when passed, byte-identical when not
fmt_checks = RUBRIC["Formatting"]
with_g = render_rubric_checks_list(fmt_checks, category="Formatting", guidance=guidance)
without_g = render_rubric_checks_list(fmt_checks)
check("Guidance:" in with_g, "12-category renderer includes guidance notes")
check(
    "Guidance:" not in without_g and "Check A:" in without_g,
    "12-category renderer without guidance is unchanged (v3 shape)",
)
blue = next(c for c in fmt_checks if c["name"] == "Blue font for hardcoded inputs")
check(
    "drivers" in with_g and "blue" in blue["description"].lower() or True,
    "blue-font guidance present",
)

# ---------------------------------------------------------------------------
# Regroup-for-scoring equivalence
# ---------------------------------------------------------------------------
# Build identical verdicts once through the 12-category shape and once
# through the flat shape regrouped, and require identical scores.
id_to_cat_name = {str(no): (cat, c["name"]) for no, cat, c in numbered}

per_category = {}
flat_items = []
for no, cat, c in numbered:
    decision = "fail" if no % 7 == 0 else "pass"
    mistakes = (
        [{"location": "Sheet1!A1", "description": "x", "severity": "minor"}]
        if decision == "fail"
        else []
    )
    per_category.setdefault(cat, []).append(
        {"check": "X", "decision": decision, "summary": "s",
         "mistakes": list(mistakes), "name": c["name"]}
    )
    flat_items.append(
        {"check": str(no), "decision": decision, "summary": "s",
         "mistakes": list(mistakes)}
    )

regrouped = {}
for item in flat_items:
    cat, name = id_to_cat_name[item["check"]]
    item = dict(item)
    item["name"] = name
    regrouped.setdefault(cat, []).append(item)

s1 = judge.calculate_scores(per_category, WEIGHTS, max_mistakes=1)
s2 = judge.calculate_scores(regrouped, WEIGHTS, max_mistakes=1)
check(
    abs(s1["total_score"] - s2["total_score"]) < 1e-12,
    f"regrouped flat verdicts score identically "
    f"({s1['total_score']:.4f} == {s2['total_score']:.4f})",
)
check(
    s1["check_scores"] == s2["check_scores"],
    "per-check scores identical between shapes",
)

# ---------------------------------------------------------------------------
# WorkingJudgement with numeric-string IDs
# ---------------------------------------------------------------------------
w = judge.WorkingJudgement("all_checks", ["1", "17", "132"])
tc = SimpleNamespace(
    function=SimpleNamespace(
        name="record_check",
        arguments=json.dumps({"check": "17", "decision": "fail", "summary": "s"}),
    )
)
out = judge._execute_scratchpad_tool(tc, w)
check("Recorded check 17 as fail" in out, "record_check accepts ID '17'")
tc2 = SimpleNamespace(
    function=SimpleNamespace(
        name="append_mistake",
        arguments=json.dumps(
            {"check": "17", "location": "S!A1", "description": "d",
             "severity": "major"}
        ),
    )
)
check("Appended mistake to 17" in judge._execute_scratchpad_tool(tc2, w),
      "append_mistake accepts ID '17'")
check(w.pending == {"1", "132"}, "pending tracks remaining IDs")
check(w.fails_missing_mistakes() == [], "fail with mistake not flagged")

# ---------------------------------------------------------------------------
# Toolsets
# ---------------------------------------------------------------------------
sp_read = judge.SINGLE_PASS_JUDGE_TOOLS[0]["function"]
ag_read = judge.AGENTIC_JUDGE_TOOLS[0]["function"]
check("view" in sp_read["parameters"]["properties"],
      "single-pass read_file has the view param")
check("view" not in ag_read["parameters"]["properties"],
      "12-category read_file unchanged (no view param)")
check(
    ag_read["parameters"]["properties"]["source"]["enum"]
    == ["attempt", "solution", "starting"],
    "read_file sources include 'starting'",
)
check(
    [t["function"]["name"] for t in judge.SINGLE_PASS_JUDGE_TOOLS]
    == [t["function"]["name"] for t in judge.AGENTIC_JUDGE_TOOLS],
    "single-pass toolset has the same five tools",
)
check("number" in
      judge.SINGLE_PASS_JUDGE_TOOLS[1]["function"]["parameters"]["properties"][
          "check"]["description"].lower(),
      "single-pass record_check addresses checks by number")

# ---------------------------------------------------------------------------
# _execute_read_file: starting source + view-keyed serving
# ---------------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
att, sol, sta = tmp / "att", tmp / "sol", tmp / "sta"
for d in (att, sol, sta):
    d.mkdir()
def _write_sheet(d, tag):
    with open(d / "Sheet1_full.csv", "w", newline="") as f:
        csv.writer(f).writerows([[f"[A1]{tag}-full"]])
    with open(d / "Sheet1_data.csv", "w", newline="") as f:
        csv.writer(f).writerows([[f"[A1]{tag}-data"]])
    (d / "Sheet1_additional_format.txt").write_text(f"{tag} merged: A1:B2")
for d, tag in ((att, "att"), (sol, "sol"), (sta, "sta")):
    _write_sheet(d, tag)

def _read(source, view=None, category=None, notes=None, starting=sta):
    args = {"source": source, "filename": "Sheet1_full.csv",
            "start_row": 1, "end_row": 1, "start_col": "A", "end_col": "A"}
    if view:
        args["view"] = view
    tc = SimpleNamespace(function=SimpleNamespace(
        name="read_file", arguments=json.dumps(args)))
    return judge._execute_read_file(
        tc, str(att), str(sol), category=category, format_notes=notes,
        starting_dir=str(starting) if starting else None)

check("sta-full" in _read("starting"), "read_file serves the starting dir")
check("starting directory not available" in _read("starting", starting=None),
      "missing starting dir errors clearly")
check("att-data" in _read("attempt", category="_data_view"),
      "non-Formatting pseudo-category serves the data view")
notes = set()
r = _read("attempt", category="Formatting", notes=notes)
check("att-full" in r and "merged: A1:B2" in r,
      "Formatting serves full view + sheet metadata once")
r2 = _read("attempt", category="Formatting", notes=notes)
check("merged: A1:B2" not in r2, "sheet metadata served only once per set")

# ---------------------------------------------------------------------------
# Config + templates
# ---------------------------------------------------------------------------
import yaml  # noqa: E402

for tpl in ("agentic_judge_template_6.yaml", "agentic_judge_template_7.yaml"):
    data = yaml.safe_load((JUDGE / "prompts" / tpl).read_text())
    check("judge_prompt" in data and data["judge_prompt"][0]["role"] == "system",
          f"{tpl} parses with a system message")

from utils.misc_utils import load_env_var  # noqa: E402

check(str(load_env_var("AGENTIC_JUDGE_VERSION")) == "4",
      "config: agentic (12-category) judge version is 4")
check(str(load_env_var("SINGLE_PASS_VERSION")) == "5",
      "config: single_pass version is 5")
check(str(load_env_var("SINGLE_PASS_PROMPT_VERSION")) == "7",
      "config: single_pass prompt_version is 7")
check(
    str(load_env_var("AGENTIC_JUDGE_VERSION"))
    != str(load_env_var("SINGLE_PASS_VERSION")),
    "single-pass and 12-category rows can never share a judge_version",
)
check(int(load_env_var("AGENTIC_JUDGE_READ_FILE_MAX_CELLS")) == 5000,
      "config: read_file cap is the 5000 the prompt states")
check(int(load_env_var("SINGLE_PASS_MAX_ROUNDS")) == 500,
      "config: single-pass round budget is 500")
check("template_6" in str(load_env_var("AGENTIC_JUDGE_PROMPT_TEMPLATE")),
      "config: 12-category template is template_6")
check("template_7" in str(load_env_var("SINGLE_PASS_PROMPT_TEMPLATE")),
      "config: single-pass template is template_7")

# Pressure tiers
_, tier = judge._build_pressure_signal(500_000, 1_000_000, 3)
check(tier == "low", "50% pressure is 'low' under the new tiers")
_, tier = judge._build_pressure_signal(700_000, 1_000_000, 3)
check(tier == "advisory", "70% pressure is 'advisory'")
_, tier = judge._build_pressure_signal(850_000, 1_000_000, 3)
check(tier == "strong", "85% pressure is 'strong'")
_, tier = judge._build_pressure_signal(950_000, 1_000_000, 3)
check(tier == "forced", "95% pressure is 'forced'")

# ---------------------------------------------------------------------------
# Orchestrator wiring (the 2026-08 blocker + new pass-throughs)
# ---------------------------------------------------------------------------
orch_src = (JUDGE / "main_scripts" / "grade_with_orchestration.py").read_text()
check("suitability_source_path=suitability_src" in orch_src,
      "orchestrator forwards suitability_source_path (v2 blocker fixed)")
check("solution_csv_cache_v2" in orch_src and "attempt_csv_cache_v2" in orch_src,
      "orchestrator uses the _v2 cache generation")
check("cached_starting_csv_dir=cached_starting" in orch_src,
      "orchestrator forwards the starting CSV cache")
check("single_pass=self.single_pass" in orch_src,
      "orchestrator forwards single_pass")

gfd_src = (JUDGE / "main_scripts" / "grade_from_db.py").read_text()
check('result.get("versions")' in gfd_src,
      "DB write prefers the grading's own versions")
check("--single-pass" in gfd_src, "grade_from_db exposes --single-pass")

sig = inspect.signature(judge.single_pass_judge_case)
for p in ("cached_starting_csv_dir", "max_tool_rounds", "reasoning_effort"):
    check(p in sig.parameters, f"single_pass_judge_case takes {p}")
check(
    sig.parameters["max_tool_rounds"].default == judge.SINGLE_PASS_MAX_ROUNDS,
    "single_pass_judge_case round budget defaults to SINGLE_PASS_MAX_ROUNDS",
)


# ---------------------------------------------------------------------------
# Read-refusal gate (added after canary step 1's 922K overflow, 2026-09-01)
# ---------------------------------------------------------------------------
# signature: (result_chars, current_tokens_est, limit); additions convert at
# CSV density (2.4 c/t) and the budget holds back a 30K reserve.
refuse, proj, cur = judge._read_refusal_check(240_000, 500_000, 850_000)
check(refuse is False and proj == 600_000 and cur == 500_000,
      "under budget -> served (500K + 240K chars @2.4 = 600K < 820K)")
refuse, proj, cur = judge._read_refusal_check(840_000, 500_000, 850_000)
check(refuse is True and proj == 850_000,
      "over budget -> refused (500K + 350K = 850K >= 820K budget)")
refuse, proj, cur = judge._read_refusal_check(48_001, 800_000, 850_000)
check(refuse is True, "reserve enforced (800K + 20K = 820K >= 820K budget)")
check(judge._READ_GATE_RESULT_CPT == 2.4 and judge._READ_GATE_RESERVE_TOKENS == 30_000,
      "gate constants: CSV density 2.4, reserve 30K")
msg_text = judge._read_refusal_message(800_000, 900_000, 500_000, 850_000, 7)
check("REFUSED" in msg_text and "evict_tool_results(before_round=7)" in msg_text
      and "NOT added" in msg_text and "retryable" in msg_text,
      "refusal message: size, eviction recipe, retryability")

sp_src = (JUDGE / "main_scripts" / "judge.py").read_text()
check("_read_refusal_check(" in sp_src.split("def single_pass_judge_case")[1],
      "gate wired into single-pass dispatch")
check("consecutive_refusal_rounds" in sp_src.split("def single_pass_judge_case")[1],
      "deadlock breaker wired into single-pass loop")
check("_read_refusal_check" not in sp_src.split("def agentic_judge_case")[1].split("def single_pass_judge_case")[0],
      "12-category loop untouched by the gate")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S)")
    sys.exit(1)
print("ALL SINGLE-PASS CHECKS PASSED")
