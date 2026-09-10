"""Offline checks for the benchmark (v1|v2) switch and the v8/v9/v10
templates, including v10's House_Standards_v1.md attachment.

Run from coding-agents-master:  python tests/test_benchmark_config.py
(or pytest tests/). No Docker, DB, S3, or API keys needed; the v10 checks
need the monorepo (house_standards/, gui-agents-master/) around the checkout.
"""
import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coding_agent import repo_config  # noqa: E402
from coding_agent.config import (  # noqa: E402
    TEMPLATE_ATTACHMENTS,
    load_config,
    template_attachments,
)
from coding_agent.prompt_builder import (  # noqa: E402
    V8_RUBRIC_MARKER,
    build_prompt,
    parse_prompt_version,
    prompt_file_paths,
    template_name,
)
from coding_agent.run_task import _snapshot_run_inputs  # noqa: E402
from coding_agent.task_source import ExternalSource  # noqa: E402
from coding_agent.workspace import create_attempt, seed_template_attachments  # noqa: E402

STANDARDS_REL = "house_standards/House_Standards_v1.md"
GUI_STEP2_V4 = "gui-agents-master/tasks_configs/prompts_v4/step2_build.txt"


def _cfg(extra: str) -> str:
    base = """
mode: internal
agent_model_name: claudecode_anthropic/claude-haiku-4-5
"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False
    ) as f:
        f.write(base + extra)
        return f.name


def main() -> int:
    # benchmark is required for internal mode — a silent default would let a
    # config that omits the key record against the wrong benchmark.
    try:
        load_config(_cfg(""))
        raise AssertionError("internal config without benchmark should be refused")
    except ValueError as e:
        assert "benchmark is required" in str(e)
        print("OK  internal config without benchmark refused")

    # external mode has no DB/S3; omitting benchmark keeps the v1 template default.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("mode: external\nagent_model_name: claudecode_anthropic/claude-haiku-4-5\n")
        ext_path = f.name
    ext = load_config(ext_path)
    assert ext.benchmark == "v1" and ext.template_version == "v7"
    print("OK  external config without benchmark -> v1 template default")

    # v1 explicit: unchanged historical wiring.
    v1 = load_config(_cfg("benchmark: v1\n"))
    assert v1.benchmark == "v1"
    assert v1.template_version == "v7"
    assert v1.s3_root == "BizbenchV1"
    print("OK  benchmark v1 -> v7, BizbenchV1 root")

    # v2: root/template flip together (v10 = the House Standards template).
    v2 = load_config(_cfg("benchmark: v2\n"))
    assert v2.benchmark == "v2"
    assert v2.template_version == "v10"
    assert v2.s3_root == "MBABenchV2"
    assert v2.s3_bucket  # from config/config.yaml aws.s3_bucket or the default
    print("OK  benchmark v2 -> v10, MBABenchV2 root")

    # the old internal: stanza is refused, not silently honoured.
    try:
        load_config(_cfg("benchmark: v2\ninternal:\n  s3_root: BizbenchV1\n"))
        raise AssertionError("internal: should be refused")
    except ValueError as e:
        assert "internal" in str(e)
        print("OK  stale internal: stanza refused")

    # bad benchmark refused.
    try:
        load_config(_cfg("benchmark: v3\n"))
        raise AssertionError("benchmark v3 should be refused")
    except ValueError:
        print("OK  benchmark v3 refused")

    # v10 template resolves for every task_source, passes the rubric
    # checksum guard, and yields prompt_version 110.
    for src in ("fmwc", "modeloff", "wsp", "jp"):
        assert template_name(src, "v10") == "task_template_shared_v10.txt"
    sys_path, tpl_path = prompt_file_paths(v2, "jp")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 110
    print("OK  v10 template: shared across sources, checksum guard passed, pv=110")

    # v10 declares the house-standards attachment on the template, and it
    # resolves to the canonical monorepo file.
    assert TEMPLATE_ATTACHMENTS["v10"] == [STANDARDS_REL]
    root = repo_config.monorepo_root()
    assert root is not None and (root / STANDARDS_REL).is_file(), root
    [standards] = template_attachments(v2)
    assert standards == root / STANDARDS_REL
    sha = hashlib.sha256(standards.read_bytes()).hexdigest()
    assert v2.extra_configs()["house_standards"] == {
        "version": 1, "file": "House_Standards_v1.md", "sha256": sha,
    }
    print("OK  v10 attachment declared, resolvable, recorded in extra_configs")

    # v10 = v9 + the directive, pointing at the workspace path (never
    # "attached"), and QA item 9.
    v10_text = tpl_path.read_text()
    assert v10_text.count("in the workspace at starting_files/House_Standards_v1.md") == 2
    assert "HOUSE STANDARDS\n- The file House_Standards_v1.md" in v10_text
    assert "9. The workbook follows House_Standards_v1.md" in v10_text
    assert "attached" not in v10_text.split(V8_RUBRIC_MARKER)[0]
    print("OK  v10 template carries the house-standards directive")

    # v10's rubric is byte-identical to the GUI source it was generated from.
    gui_step2 = (root / GUI_STEP2_V4).read_text()
    gui_rubric = gui_step2[gui_step2.index(V8_RUBRIC_MARKER):].rstrip("\n")
    tpl_rubric = v10_text[v10_text.index(V8_RUBRIC_MARKER):][:len(gui_rubric)]
    assert tpl_rubric == gui_rubric, "v10 rubric drifted from prompts_v4/step2_build.txt"
    print("OK  v10 rubric byte-identical to the GUI prompts_v4 source")

    # v9 still selectable and guarded: pv 109, no attachments.
    v2_v9 = load_config(_cfg("benchmark: v2\ntemplate_version: v9\n"))
    sys_path, tpl_path = prompt_file_paths(v2_v9, "jp")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 109
    assert template_attachments(v2_v9) == []
    assert "house_standards" not in v2_v9.extra_configs()
    print("OK  v9 template still selectable, checksum guard passed, pv=109")

    # v9 carries the Questions-sheet convention.
    tpl_text = tpl_path.read_text()
    assert "ANSWERS (the 'Questions' sheet)" in tpl_text
    assert "8. The 'Questions' sheet is intact" in tpl_text
    assert "start solution.xlsx as a copy of the starting workbook" in tpl_text
    assert "the reserved answer cells are in the column headed 'Answers'" in tpl_text, (
        "step-1 plan bullet lost the header-anchored answer-column wording"
    )
    assert "reserved answer cells in column B (" not in tpl_text, (
        "stale hard-coded column-B plan wording survived"
    )
    print("OK  v9 template carries the Questions-sheet convention")

    # v8 still selectable and guarded: pv 108.
    v2_v8 = load_config(_cfg("benchmark: v2\ntemplate_version: v8\n"))
    sys_path, tpl_path = prompt_file_paths(v2_v8, "jp")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 108
    print("OK  v8 template still selectable, checksum guard passed, pv=108")

    # v7 unchanged: still pv 107 with its own guard.
    sys_path, tpl_path = prompt_file_paths(v1, "fmwc")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 107
    print("OK  v7 template unchanged, pv=107")

    check_seeded_workspace()

    print("ALL BENCHMARK CONFIG CHECKS PASSED")
    return 0


def check_seeded_workspace() -> None:
    """A v10 workspace holds starting_files/House_Standards_v1.md, PROMPT.md
    lists it, the prompts/ snapshot carries it, and a task input of the
    same name is refused rather than shadowed."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        task = tmp / "task"
        (task / "starting_files").mkdir(parents=True)
        (task / "task.yaml").write_text("task_name: seeded\ntask_source: jp\n")
        (task / "starting_files" / "case.xlsx").write_bytes(b"not really xlsx")
        run_yaml = tmp / "run.yaml"
        run_yaml.write_text(
            "mode: external\nbenchmark: v2\n"
            "agent_model_name: claudecode_anthropic/claude-haiku-4-5\n"
        )
        cfg = load_config(run_yaml)
        cfg.workspaces_dir = tmp / "workspaces"
        assert cfg.template_version == "v10"

        spec = ExternalSource(task).fetch(tmp / "_staging")
        seeded = seed_template_attachments(cfg, spec)
        assert [p.name for p in seeded] == ["House_Standards_v1.md"]
        attempt = create_attempt(cfg.workspaces_dir, spec)
        _snapshot_run_inputs(cfg, spec, attempt)

        ws_copy = attempt.workspace / "starting_files" / "House_Standards_v1.md"
        assert ws_copy.read_bytes() == seeded[0].read_bytes()
        assert "starting_files/House_Standards_v1.md" in attempt.manifest
        prompt, pv = build_prompt(cfg, spec, attempt.workspace)
        assert pv == 110
        assert "- starting_files/House_Standards_v1.md (" in prompt
        assert (attempt.attempt_dir / "prompts" / "House_Standards_v1.md").exists()
        assert (attempt.attempt_dir / "prompts" / "task_template_shared_v10.txt").exists()
        print("OK  v10 workspace seeded with House_Standards_v1.md; PROMPT.md lists it")

        (task / "starting_files" / "House_Standards_v1.md").write_bytes(b"impostor")
        spec2 = ExternalSource(task).fetch(tmp / "_staging2")
        try:
            seed_template_attachments(cfg, spec2)
            raise AssertionError("a task input shadowing the attachment should be refused")
        except FileExistsError:
            print("OK  attachment/task-file name collision refused")

        # A declared-but-missing attachment fails before any workspace exists.
        cfg.template_version = "v10"
        try:
            TEMPLATE_ATTACHMENTS["v10"] = ["house_standards/House_Standards_v999.md"]
            try:
                template_attachments(cfg)
                raise AssertionError("missing attachment should be refused")
            except FileNotFoundError:
                print("OK  missing declared attachment refused")
        finally:
            TEMPLATE_ATTACHMENTS["v10"] = [STANDARDS_REL]


def test_benchmark_config():
    """pytest entry point; the script form prints as it goes."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
