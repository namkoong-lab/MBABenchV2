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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✅ {name}")
    print("All workspace-guard tests passed.")
