"""The v2 prompt sets (and their registry entries) must stay BYTE-IDENTICAL
to the gui pipeline's.

Identical prompts + identical rubric are what make a gui-vs-excel score
delta attributable to the interface rather than the text. If the gui files
change, this fails — decide deliberately: either re-copy under a NEW
version number here (never mutate a version once it has recorded runs) or
accept divergence and delete this guard on purpose.
"""

from pathlib import Path

from infra.configs.prompt_registry import load_registry

MEMBER_ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = MEMBER_ROOT.parent / "gui-agents-master"

# version -> the files it must share byte-identically with the gui copy.
SHARED_SETS = {
    200: [
        "tasks_configs/prompts_v2/step1_analyze.txt",
        "tasks_configs/prompts_v2/step2_build.txt",
        "tasks_configs/prompts_v2/step3_qa.txt",
    ],
    202: [
        "tasks_configs/prompts_v3/step1_analyze.txt",
        "tasks_configs/prompts_v3/step2_build.txt",
        "tasks_configs/prompts_v3/step3_qa.txt",
    ],
    203: [
        "tasks_configs/prompts/v2_2.txt",
    ],
    204: [
        "tasks_configs/prompts_v4/step1_analyze.txt",
        "tasks_configs/prompts_v4/step2_build.txt",
        "tasks_configs/prompts_v4/step3_qa.txt",
    ],
    205: [
        "tasks_configs/prompts/v2_3.txt",
    ],
}
# version -> attachments both registries must declare (repo-root-relative,
# so the same string reaches the same monorepo file from either member).
SHARED_ATTACHMENTS = {
    204: ["../house_standards/House_Standards_v1.md"],
    205: ["../house_standards/House_Standards_v1.md"],
}


def test_shared_sets_byte_identical_to_gui():
    assert GUI_ROOT.exists(), (
        "gui-agents-master not found next to excel-agents-master — this "
        "guard only runs inside the monorepo"
    )
    for version, files in SHARED_SETS.items():
        for rel in files:
            ours = (MEMBER_ROOT / rel).read_bytes()
            theirs = (GUI_ROOT / rel).read_bytes()
            assert ours == theirs, f"[{version}] {rel} diverged from the gui copy"


def test_registry_resolves_shared_sets():
    registry = load_registry()
    for version, files in SHARED_SETS.items():
        assert version in registry, f"version {version} (shared rubric-v9 set) must exist"
        assert list(registry[version].files) == files
    for version, entry in registry.items():
        for rel in entry.files:
            p = MEMBER_ROOT / rel
            assert p.exists(), f"version {version} references missing file {rel}"
            assert p.stat().st_size > 0, f"version {version} file is empty: {rel}"


def test_registry_entries_match_gui_registry():
    """The shared versions must be registered IDENTICALLY on both sides —
    same files, same attachments — or the two cohorts' rows would carry
    the same number for different deliveries."""
    ours = load_registry()
    theirs = load_registry(GUI_ROOT / "tasks_configs/prompts/registry.yaml")
    for version in SHARED_SETS:
        assert version in theirs, f"gui registry lacks version {version}"
        assert ours[version].files == theirs[version].files, version
        assert ours[version].attachments == theirs[version].attachments, version
    for version, attachments in SHARED_ATTACHMENTS.items():
        assert list(ours[version].attachments) == attachments, version
        for rel in attachments:
            for root in (MEMBER_ROOT, GUI_ROOT):
                target = (root / rel).resolve()
                assert target.is_file(), f"[{version}] {rel} missing from {root}"
    # Versions without a declared attachment attach nothing.
    for version in SHARED_SETS.keys() - SHARED_ATTACHMENTS.keys():
        assert ours[version].attachments == ()


def test_house_standards_versions_keep_rubric_body():
    """204/205 add the house standards ABOVE the rubric only: the rubric
    body must stay byte-identical to 202/203's so a score delta is
    attributable to the standards alone."""
    marker = b"== FULL RUBRIC"

    def body(rel):
        data = (MEMBER_ROOT / rel).read_bytes()
        return data[data.index(marker):]

    assert body("tasks_configs/prompts_v4/step2_build.txt") == body(
        "tasks_configs/prompts_v3/step2_build.txt"
    )
    assert body("tasks_configs/prompts/v2_3.txt") == body(
        "tasks_configs/prompts/v2_2.txt"
    )
    for rel in SHARED_SETS[204] + SHARED_SETS[205]:
        assert b"House_Standards_v1.md" in (MEMBER_ROOT / rel).read_bytes(), rel


def test_202_rubric_body_revised_from_200():
    # The 2026-08 rubric revision (Patrick-approved in-place text update from
    # the canonical checklist xlsx) deliberately changed the live 202/203
    # rubric bodies while leaving the frozen 200/201 sets untouched. The two
    # live sets must carry the SAME revised rubric; the frozen 200 set must
    # NOT match it.
    marker = b"== FULL RUBRIC"
    v2 = (MEMBER_ROOT / "tasks_configs/prompts_v2/step2_build.txt").read_bytes()
    v3 = (MEMBER_ROOT / "tasks_configs/prompts_v3/step2_build.txt").read_bytes()
    v2_2 = (MEMBER_ROOT / "tasks_configs/prompts/v2_2.txt").read_bytes()
    assert v3[v3.index(marker):] == v2_2[v2_2.index(marker):], (
        "the 202 (prompts_v3) and 203 (v2_2.txt) rubric bodies diverged — "
        "rerun judge/operation_scripts/build_rubric_9_from_xlsx.py"
    )
    assert v2[v2.index(marker):] != v3[v3.index(marker):], (
        "the live 202 rubric matches the frozen 200 rubric — the 2026-08 "
        "revision text is missing"
    )


def test_smoke_version_exists():
    registry = load_registry()
    assert 0 in registry, "the pipeline smoke prompt (version 0) must exist"
