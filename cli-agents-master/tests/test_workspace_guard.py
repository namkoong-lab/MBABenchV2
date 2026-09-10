"""Offline tests for the setup_workspace empty-context guards (no API, no S3).

Covers: hard failure on S3 download error, on zero-byte downloads, on invalid
S3 URIs, on tasks with no registered starting files, and the batch-start S3
preflight — the guards added after the July empty-context defect (silent S3
download failures let agents run and record attempts with no task content).

Run:  python tests/test_workspace_guard.py   (or pytest)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from excel_cli_agent.auto_batch_runner import AutoBatchRunner, TaskInfo


def make_runner(tmp_path, s3_client):
    """Bare runner for exercising workspace setup without config/DB/API."""
    runner = object.__new__(AutoBatchRunner)
    runner.config = {'workspace_base_dir': str(tmp_path)}
    runner._s3_client = s3_client
    runner._s3_bucket = "mbabench"
    runner._attachments = []
    return runner


def make_task(starting_files):
    return TaskInfo(
        task_id=999,
        task_name="Guard Test",
        task_source="fmwc",
        task_starting_files=starting_files,
    )


class FailingS3:
    def download_file(self, bucket, key, path):
        raise Exception("Unable to locate credentials")

    def head_bucket(self, Bucket):
        raise Exception("Unable to locate credentials")


class ZeroByteS3:
    def download_file(self, bucket, key, path):
        Path(path).write_bytes(b"")


class GoodS3:
    def download_file(self, bucket, key, path):
        Path(path).write_bytes(b"PK\x03\x04 not really an xlsx")

    def head_bucket(self, Bucket):
        return {}


def expect_raises(fn, fragment):
    try:
        fn()
    except RuntimeError as e:
        assert fragment in str(e), f"expected {fragment!r} in {e}"
        return
    raise AssertionError(f"expected RuntimeError containing {fragment!r}")


def test_download_failure_raises():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, FailingS3())
        task = make_task(["s3://mbabench/BizbenchV1/tasks/x/starting_files/a.xlsx"])
        expect_raises(lambda: runner.setup_workspace(task), "Failed to download")


def test_zero_byte_download_raises():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, ZeroByteS3())
        task = make_task(["s3://mbabench/BizbenchV1/tasks/x/starting_files/a.xlsx"])
        expect_raises(lambda: runner.setup_workspace(task), "zero-byte")


def test_invalid_uri_raises():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, GoodS3())
        task = make_task(["/local/path/a.xlsx"])
        expect_raises(lambda: runner.setup_workspace(task), "Invalid S3 URI")


def test_no_starting_files_raises():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, GoodS3())
        task = make_task([])
        expect_raises(lambda: runner.setup_workspace(task), "no task_starting_files")


def test_successful_download_passes():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, GoodS3())
        task = make_task(["s3://mbabench/BizbenchV1/tasks/x/starting_files/a.xlsx"])
        workspace = runner.setup_workspace(task)
        assert (Path(workspace) / "a.xlsx").stat().st_size > 0


def test_workspace_defaults_to_batch_logs_dir():
    """Without workspace_base_dir, workspaces live under the batch's logs dir."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, GoodS3())
        runner.config = {}
        runner.batch_logs_dir = Path(tmp) / "batch_logs" / "batch_x"
        task = make_task(["s3://mbabench/MBABenchV2/tasks/x/starting_files/a.xlsx"])
        workspace = runner.setup_workspace(task)
        assert Path(workspace).parent == runner.batch_logs_dir / "workspaces"
        assert (Path(workspace) / "a.xlsx").stat().st_size > 0


def test_no_workspace_location_raises():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, GoodS3())
        runner.config = {}
        runner.batch_logs_dir = None
        task = make_task(["s3://mbabench/MBABenchV2/tasks/x/starting_files/a.xlsx"])
        expect_raises(lambda: runner.setup_workspace(task), "No workspace location")


def test_s3_preflight_raises_without_credentials():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, FailingS3())
        expect_raises(runner._verify_s3_access, "Cannot access s3://mbabench")


def test_s3_preflight_passes():
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(tmp, GoodS3())
        runner._verify_s3_access()


def test_detected_pdfs_resolve_from_relative_workspace():
    """PDF detection must yield bare names: consumers join them onto the
    workspace path, so a prefixed relative path silently resolved to a
    missing file and dropped every PDF from context (empty-PDF defect)."""
    import os
    from excel_cli_agent.batch_runner import BatchRunner

    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            ws = Path("workspaces_rel/Task_X_1_2")
            ws.mkdir(parents=True)
            (ws / "Case Materials.pdf").write_bytes(b"%PDF-1.4 stub")
            (ws / "Model.xlsx").write_bytes(b"PK stub")

            runner = object.__new__(BatchRunner)
            cfg = runner.detect_workspace_files(str(ws))

            assert cfg.detected_pdf_files == ["Case Materials.pdf"], cfg.detected_pdf_files
            assert cfg.detected_excel_files == ["Model.xlsx"], cfg.detected_excel_files
            # the join every consumer performs must hit the real file
            for name in cfg.detected_pdf_files + cfg.detected_excel_files:
                assert (Path(str(ws)) / name).exists(), name
        finally:
            os.chdir(old_cwd)


# --- prompt-version attachments (v14 house standards) ---

def _write_standards(dir_path):
    p = Path(dir_path) / "House_Standards_v1.md"
    p.write_text("# Financial Modelling - House Standards\n\nZeros as dashes.\n")
    return p


def test_detected_md_files_are_text_context():
    """.md files are detected as text context, bare names like the rest."""
    from excel_cli_agent.batch_runner import BatchRunner

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "Task_X"
        ws.mkdir()
        _write_standards(ws)
        (ws / "Model.xlsx").write_bytes(b"PK stub")
        runner = object.__new__(BatchRunner)
        cfg = runner.detect_workspace_files(str(ws))
        assert cfg.detected_text_files == ["House_Standards_v1.md"], cfg.detected_text_files
        assert cfg.detected_excel_files == ["Model.xlsx"]
        assert (ws / cfg.detected_text_files[0]).exists()


def test_attachment_copied_into_workspace():
    """setup_workspace lands each attachment under its bare filename."""
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "house_standards"
        src_dir.mkdir()
        src = _write_standards(src_dir)
        runner = make_runner(Path(tmp) / "ws", GoodS3())
        runner._attachments = [src]
        task = make_task(["s3://mbabench/MBABenchV2/tasks/x/starting_files/a.xlsx"])
        workspace = Path(runner.setup_workspace(task))
        assert (workspace / "a.xlsx").stat().st_size > 0
        assert (workspace / "House_Standards_v1.md").read_text() == src.read_text()
        # the copy is what the executor will detect and embed
        cfg = runner.detect_workspace_files(str(workspace))
        assert cfg.detected_text_files == ["House_Standards_v1.md"]


def test_missing_attachment_raises():
    """An attachment the prompt version promises but that is gone is fatal."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = make_runner(Path(tmp) / "ws", GoodS3())
        runner._attachments = [Path(tmp) / "house_standards" / "House_Standards_v1.md"]
        task = make_task(["s3://mbabench/MBABenchV2/tasks/x/starting_files/a.xlsx"])
        expect_raises(lambda: runner.setup_workspace(task), "Prompt attachment missing")


def test_empty_attachment_raises():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "House_Standards_v1.md"
        src.write_bytes(b"")
        runner = make_runner(Path(tmp) / "ws", GoodS3())
        runner._attachments = [src]
        task = make_task(["s3://mbabench/MBABenchV2/tasks/x/starting_files/a.xlsx"])
        expect_raises(lambda: runner.setup_workspace(task), "Prompt attachment missing or empty")


def test_local_runner_copies_attachments():
    """Local mode ships the same attachments after copying the source folder."""
    from excel_cli_agent.local_batch_runner import LocalBatchRunner

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "task_src"
        source.mkdir()
        (source / "Model.xlsx").write_bytes(b"PK stub")
        src = _write_standards(Path(tmp))
        runner = object.__new__(LocalBatchRunner)
        runner.config = {'workspace_base_dir': str(Path(tmp) / "ws")}
        runner._attachments = [src]
        workspace = Path(runner.setup_workspace(str(source)))
        assert (workspace / "Model.xlsx").exists()
        assert (workspace / "House_Standards_v1.md").read_text() == src.read_text()


def test_attachment_provenance_and_v14_resolution():
    """v14 resolves the real house-standards file from the monorepo root and
    records {version, file, sha256} for extra_configs; v13 attaches nothing."""
    import hashlib
    from excel_cli_agent import prompt_versions, repo_config

    assert prompt_versions.attachments_for("v13") == []
    rels = prompt_versions.attachments_for("v14")
    assert rels == ["house_standards/House_Standards_v1.md"]
    paths = repo_config.resolve_attachments(rels)
    assert paths[0].name == "House_Standards_v1.md" and paths[0].is_file()
    prov = repo_config.attachment_extra_configs(paths)
    assert prov["house_standards"]["version"] == 1
    assert prov["house_standards"]["file"] == "House_Standards_v1.md"
    assert prov["house_standards"]["sha256"] == hashlib.sha256(paths[0].read_bytes()).hexdigest()
    # a missing declared file fails before any task is claimed
    expect_raises(lambda: repo_config.resolve_attachments(["house_standards/House_Standards_v999.md"]),
                  "missing or empty")


def test_v15_delivers_house_standards_under_the_directive_name():
    """v15 lands the standards as HOUSE_STANDARDS.md — the name the prompt
    points at and the coding pipeline uses — and provenance records both
    the delivered name and the versioned source."""
    import hashlib
    from excel_cli_agent import prompt_versions, repo_config

    assert prompt_versions.attachment_names_for("v14") == {}
    names = prompt_versions.attachment_names_for("v15")
    assert names == {"House_Standards_v1.md": "HOUSE_STANDARDS.md"}
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_standards(Path(tmp))
        runner = make_runner(Path(tmp) / "ws", GoodS3())
        runner._attachments = [src]
        runner._attachment_names = names
        task = make_task(["s3://mbabench/MBABenchV2/tasks/x/starting_files/a.xlsx"])
        workspace = Path(runner.setup_workspace(task))
        assert (workspace / "HOUSE_STANDARDS.md").read_text() == src.read_text()
        assert not (workspace / "House_Standards_v1.md").exists()
        cfg = runner.detect_workspace_files(str(workspace))
        assert cfg.detected_text_files == ["HOUSE_STANDARDS.md"]
        prov = repo_config.attachment_extra_configs([src], names)["house_standards"]
        assert prov == {"version": 1, "file": "HOUSE_STANDARDS.md", "source": "House_Standards_v1.md",
                        "sha256": hashlib.sha256(src.read_bytes()).hexdigest()}
        assert runner._attachment_extra_configs()["house_standards"]["file"] == "HOUSE_STANDARDS.md"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✅ {name}")
    print("All workspace-guard tests passed.")
