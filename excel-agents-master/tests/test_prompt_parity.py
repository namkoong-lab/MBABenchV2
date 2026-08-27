"""The v2 prompt sets must stay BYTE-IDENTICAL to the gui pipeline's.

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


def test_202_rubric_body_unchanged_from_200():
    marker = b"== FULL RUBRIC"
    v2 = (MEMBER_ROOT / "tasks_configs/prompts_v2/step2_build.txt").read_bytes()
    v3 = (MEMBER_ROOT / "tasks_configs/prompts_v3/step2_build.txt").read_bytes()
    assert v2[v2.index(marker):] == v3[v3.index(marker):], (
        "the 202 rubric body drifted from 200's — the Questions-sheet "
        "revision must not touch the rubric"
    )


def test_smoke_version_exists():
    registry = load_registry()
    assert 0 in registry, "the pipeline smoke prompt (version 0) must exist"
