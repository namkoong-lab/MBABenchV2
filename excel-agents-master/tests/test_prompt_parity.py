"""The v2 prompt set must stay BYTE-IDENTICAL to the gui pipeline's.

Identical prompts + identical rubric are what make a gui-vs-excel score
delta attributable to the interface rather than the text. If the gui files
change, this fails — decide deliberately: either re-copy under a NEW
version number here (never mutate 200 once it has recorded runs) or accept
divergence and delete this guard on purpose.
"""

from pathlib import Path

from infra.configs.prompt_registry import load_registry

MEMBER_ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = MEMBER_ROOT.parent / "gui-agents-master"

SHARED_FILES = [
    "tasks_configs/prompts_v2/step1_analyze.txt",
    "tasks_configs/prompts_v2/step2_build.txt",
    "tasks_configs/prompts_v2/step3_qa.txt",
]


def test_prompts_v2_byte_identical_to_gui():
    assert GUI_ROOT.exists(), (
        "gui-agents-master not found next to excel-agents-master — this "
        "guard only runs inside the monorepo"
    )
    for rel in SHARED_FILES:
        ours = (MEMBER_ROOT / rel).read_bytes()
        theirs = (GUI_ROOT / rel).read_bytes()
        assert ours == theirs, f"{rel} diverged from the gui copy"


def test_registry_resolves_shared_set():
    registry = load_registry()
    assert 200 in registry, "version 200 (the shared rubric-v9 set) must exist"
    assert list(registry[200].files) == SHARED_FILES
    for version, entry in registry.items():
        for rel in entry.files:
            p = MEMBER_ROOT / rel
            assert p.exists(), f"version {version} references missing file {rel}"
            assert p.stat().st_size > 0, f"version {version} file is empty: {rel}"


def test_smoke_version_exists():
    registry = load_registry()
    assert 0 in registry, "the pipeline smoke prompt (version 0) must exist"
