"""Offline checks for the benchmark (v1|v2) switch and the v8/v9/v12/v13
templates, including v12's House_Standards_v1.md attachment and v13's
HOUSE_STANDARDS.md workspace extra, plus the Stage 5 ablation arms v14/v15
(re-cuts of v9/v10 under new numbers that stage nothing).

Run from coding-agents-master:  python tests/test_benchmark_config.py
(or pytest tests/). No Docker, DB, S3, or API keys needed; the v12 checks
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

    # v2: root/template flip together (v13 = the rubric-free House Standards template).
    v2_default = load_config(_cfg("benchmark: v2\n"))
    assert v2_default.benchmark == "v2"
    assert v2_default.template_version == "v13"
    assert v2_default.s3_root == "SpreadsheetSmith"
    # The bucket has no default: it is whatever aws.s3_bucket says, and a
    # checkout without one refuses rather than guessing (offline runs never ask).
    try:
        assert v2_default.s3_bucket
    except ValueError as e:
        assert "no object store configured" in str(e)
    sys_path, tpl_path = prompt_file_paths(v2_default, "v2")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 113
    assert template_attachments(v2_default) == []  # v13 stages a workspace extra instead
    assert V8_RUBRIC_MARKER not in tpl_path.read_text()
    assert v2_default.extra_configs()["house_standards"]["delivered_as"] == "HOUSE_STANDARDS.md"
    print("OK  benchmark v2 -> v13 (pv 113, rubric-free, HOUSE_STANDARDS.md extra), SpreadsheetSmith root")

    # v12 (superseded 2026-09-10) still selectable with its attachment route.
    v2 = load_config(_cfg("benchmark: v2\ntemplate_version: v12\n"))
    assert v2.template_version == "v12"

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

    # v12 template resolves for every task_source, passes the rubric
    # checksum guard, and yields prompt_version 112.
    for src in ("fmwc", "modeloff", "wsp", "v2"):
        assert template_name(src, "v12") == "task_template_shared_v12.txt"
    sys_path, tpl_path = prompt_file_paths(v2, "v2")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 112
    print("OK  v12 template still selectable: shared across sources, checksum guard passed, pv=112")

    # v12 declares the house-standards attachment on the template, and it
    # resolves to the canonical monorepo file.
    assert TEMPLATE_ATTACHMENTS["v12"] == [STANDARDS_REL]
    root = repo_config.monorepo_root()
    assert root is not None and (root / STANDARDS_REL).is_file(), root
    [standards] = template_attachments(v2)
    assert standards == root / STANDARDS_REL
    sha = hashlib.sha256(standards.read_bytes()).hexdigest()
    assert v2.extra_configs()["house_standards"] == {
        "version": 1, "file": "House_Standards_v1.md", "sha256": sha,
    }
    print("OK  v12 attachment declared, resolvable, recorded in extra_configs")

    # v12 = v9 + the directive, pointing at the workspace path (never
    # "attached"), and QA item 9.
    v12_text = tpl_path.read_text()
    assert v12_text.count("in the workspace at starting_files/House_Standards_v1.md") == 2
    assert "HOUSE STANDARDS\n- The file House_Standards_v1.md" in v12_text
    assert "9. The workbook follows House_Standards_v1.md" in v12_text
    assert "attached" not in v12_text.split(V8_RUBRIC_MARKER)[0]
    print("OK  v12 template carries the house-standards directive")

    # (No byte-equality check against gui prompts_v4 any more: 204/205 were
    # re-cut rubric-free on 2026-09-10, so that source no longer carries the
    # rubric block v12 embeds. v12's own md5 guard above still pins its text.)

    # v9 still selectable and guarded: pv 109, no attachments.
    v2_v9 = load_config(_cfg("benchmark: v2\ntemplate_version: v9\n"))
    sys_path, tpl_path = prompt_file_paths(v2_v9, "v2")
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
    sys_path, tpl_path = prompt_file_paths(v2_v8, "v2")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 108
    print("OK  v8 template still selectable, checksum guard passed, pv=108")

    # v7 unchanged: still pv 107 with its own guard.
    sys_path, tpl_path = prompt_file_paths(v1, "fmwc")
    assert parse_prompt_version(sys_path.name, tpl_path.name) == 107
    print("OK  v7 template unchanged, pv=107")

    check_seeded_workspace()
    check_v13_workspace()
    check_v14_v15_arms()

    print("ALL BENCHMARK CONFIG CHECKS PASSED")
    return 0


def check_seeded_workspace() -> None:
    """A v12 workspace holds starting_files/House_Standards_v1.md, PROMPT.md
    lists it, the prompts/ snapshot carries it, and a task input of the
    same name is refused rather than shadowed."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        task = tmp / "task"
        (task / "starting_files").mkdir(parents=True)
        (task / "task.yaml").write_text("task_name: seeded\ntask_source: v2\n")
        (task / "starting_files" / "case.xlsx").write_bytes(b"not really xlsx")
        run_yaml = tmp / "run.yaml"
        run_yaml.write_text(
            "mode: external\nbenchmark: v2\ntemplate_version: v12\n"
            "agent_model_name: claudecode_anthropic/claude-haiku-4-5\n"
        )
        cfg = load_config(run_yaml)
        cfg.workspaces_dir = tmp / "workspaces"
        assert cfg.template_version == "v12"

        spec = ExternalSource(task).fetch(tmp / "_staging")
        seeded = seed_template_attachments(cfg, spec)
        assert [p.name for p in seeded] == ["House_Standards_v1.md"]
        attempt = create_attempt(cfg.workspaces_dir, spec)
        _snapshot_run_inputs(cfg, spec, attempt)

        ws_copy = attempt.workspace / "starting_files" / "House_Standards_v1.md"
        assert ws_copy.read_bytes() == seeded[0].read_bytes()
        assert "starting_files/House_Standards_v1.md" in attempt.manifest
        prompt, pv = build_prompt(cfg, spec, attempt.workspace)
        assert pv == 112
        assert "- starting_files/House_Standards_v1.md (" in prompt
        assert (attempt.attempt_dir / "prompts" / "House_Standards_v1.md").exists()
        assert (attempt.attempt_dir / "prompts" / "task_template_shared_v12.txt").exists()
        print("OK  v12 workspace seeded with House_Standards_v1.md; PROMPT.md lists it")

        (task / "starting_files" / "House_Standards_v1.md").write_bytes(b"impostor")
        spec2 = ExternalSource(task).fetch(tmp / "_staging2")
        try:
            seed_template_attachments(cfg, spec2)
            raise AssertionError("a task input shadowing the attachment should be refused")
        except FileExistsError:
            print("OK  attachment/task-file name collision refused")

        # A declared-but-missing attachment fails before any workspace exists.
        cfg.template_version = "v12"
        try:
            TEMPLATE_ATTACHMENTS["v12"] = ["house_standards/House_Standards_v999.md"]
            try:
                template_attachments(cfg)
                raise AssertionError("missing attachment should be refused")
            except FileNotFoundError:
                print("OK  missing declared attachment refused")
        finally:
            TEMPLATE_ATTACHMENTS["v12"] = [STANDARDS_REL]


def check_v13_workspace() -> None:
    """The v2 default (v13) stages HOUSE_STANDARDS.md into the workspace root,
    seeds nothing into starting_files/, lists it in PROMPT.md, snapshots the
    source beside the prompt files, and carries no rubric."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        task = tmp / "task"
        (task / "starting_files").mkdir(parents=True)
        (task / "task.yaml").write_text("task_name: seeded13\ntask_source: v2\n")
        (task / "starting_files" / "case.xlsx").write_bytes(b"not really xlsx")
        run_yaml = tmp / "run.yaml"
        run_yaml.write_text(
            "mode: external\nbenchmark: v2\n"
            "agent_model_name: claudecode_anthropic/claude-haiku-4-5\n"
        )
        cfg = load_config(run_yaml)
        cfg.workspaces_dir = tmp / "workspaces"
        assert cfg.template_version == "v13"
        spec = ExternalSource(task).fetch(tmp / "_staging")
        assert seed_template_attachments(cfg, spec) == []
        attempt = create_attempt(cfg.workspaces_dir, spec)
        _snapshot_run_inputs(cfg, spec, attempt)
        prompt, pv = build_prompt(cfg, spec, attempt.workspace, attempt=attempt)
        assert pv == 113
        house = attempt.workspace / "HOUSE_STANDARDS.md"
        canonical = ROOT.parent / STANDARDS_REL
        assert house.read_bytes() == canonical.read_bytes()
        assert not (attempt.workspace / "starting_files" / "House_Standards_v1.md").exists()
        assert "- HOUSE_STANDARDS.md (" in prompt and "HOUSE_STANDARDS.md" in attempt.manifest
        assert V8_RUBRIC_MARKER not in prompt and "ACCURACY" not in prompt
        assert (attempt.attempt_dir / "prompts" / "house_standards_v1.md").exists()
        assert (attempt.attempt_dir / "prompts" / "task_template_shared_v13.txt").exists()
        assert cfg.extra_configs()["house_standards"]["sha256"] == hashlib.sha256(canonical.read_bytes()).hexdigest()
        print("OK  v13 workspace: HOUSE_STANDARDS.md staged at root, PROMPT.md lists it, rubric-free, provenance stamped")


def check_v14_v15_arms() -> None:
    """Stage 5 prompt ablation (2026-09-23): v14 is the v9 text byte-identical
    (rubric added back, no house standards) and v15 the v10 text byte-identical
    (the production prompt minus the house standards), each under a new
    prompt_version (114 / 115) on the production identity. Neither stages or
    seeds anything, so a row carries no house_standards provenance, and each
    passes its own guard (v14: whole-file pin + v9's rubric-section guard;
    v15: SCRUBBED_MD5 pin + rubric-free words)."""
    import subprocess
    from coding_agent.prompt_builder import RECUT_MD5, SCRUBBED_MD5
    prompts = ROOT / "coding_agent" / "prompts"
    for version, pv, source in (("v14", 114, "v9"), ("v15", 115, "v10")):
        cfg = load_config(_cfg(f"benchmark: v2\ntemplate_version: {version}\n"))
        assert cfg.template_version == version and cfg.s3_root == "SpreadsheetSmith"
        for src in ("fmwc", "modeloff", "wsp", "v2"):
            assert template_name(src, version) == f"task_template_shared_{version}.txt"
        sys_path, tpl_path = prompt_file_paths(cfg, "v2")  # md5 pin + (v14) rubric-section / (v15) rubric-free guards
        assert parse_prompt_version(sys_path.name, tpl_path.name) == pv
        assert tpl_path.read_bytes() == (prompts / f"task_template_shared_{source}.txt").read_bytes(), \
            f"{version} must be {source} byte-identical"
        assert template_attachments(cfg) == [] and "house_standards" not in cfg.extra_configs()
        text = tpl_path.read_text()
        assert "HOUSE_STANDARDS" not in text and "House_Standards" not in text
        assert "ANSWERS (the 'Questions' sheet)" in text  # the judge's answer check needs the Questions-sheet mechanics
        if version == "v14":
            assert V8_RUBRIC_MARKER in text and tpl_path.name in RECUT_MD5 and tpl_path.name not in SCRUBBED_MD5
        else:
            assert V8_RUBRIC_MARKER not in text and "rubric" not in text.lower() and tpl_path.name in SCRUBBED_MD5

        # A workspace built on the arm: PROMPT.md carries the arm's text, nothing
        # is staged into the root or starting_files/, the snapshot holds the template.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            task = tmp / "task"
            (task / "starting_files").mkdir(parents=True)
            (task / "task.yaml").write_text(f"task_name: arm{version}\ntask_source: v2\n")
            (task / "starting_files" / "case.xlsx").write_bytes(b"not really xlsx")
            run_yaml = tmp / "run.yaml"
            run_yaml.write_text(
                f"mode: external\nbenchmark: v2\ntemplate_version: {version}\n"
                "agent_model_name: claudecode_anthropic/claude-haiku-4-5\n"
            )
            ext = load_config(run_yaml)
            ext.workspaces_dir = tmp / "workspaces"
            spec = ExternalSource(task).fetch(tmp / "_staging")
            assert seed_template_attachments(ext, spec) == []
            attempt = create_attempt(ext.workspaces_dir, spec)
            _snapshot_run_inputs(ext, spec, attempt)
            prompt, got = build_prompt(ext, spec, attempt.workspace, attempt=attempt)
            assert got == pv
            assert not (attempt.workspace / "HOUSE_STANDARDS.md").exists()
            assert not (attempt.workspace / "starting_files" / "House_Standards_v1.md").exists()
            assert "HOUSE_STANDARDS" not in prompt and "HOUSE_STANDARDS.md" not in attempt.manifest
            assert (V8_RUBRIC_MARKER in prompt) == (version == "v14")
            assert (attempt.attempt_dir / "prompts" / f"task_template_shared_{version}.txt").exists()
        print(f"OK  {version} = {source} byte-identical, pv={pv}, guard passed, nothing staged, no house_standards provenance")

    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_v14_v15_templates.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    print("OK  tools/build_v14_v15_templates.py --check reproduces both arms")


def test_benchmark_config():
    """pytest entry point; the script form prints as it goes."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
