"""Offline tests for the monorepo-config resolution ladder.

Run from coding-agents-master:  python tests/test_repo_config.py
Everything runs against a throwaway config directory selected via
MBABENCH_CONFIG_DIR — no real config/config.yaml, database, or AWS is
touched. Checks that need the shared `config` module (installed by the
MBABenchV2 workspace) are skipped where it is absent.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coding_agent import repo_config  # noqa: E402
from coding_agent.config import load_config, resolve_secrets  # noqa: E402

HAVE_CONFIG_MODULE = importlib.util.find_spec("config") is not None

V1_URL = "postgresql://user:secret@db.example.com/BizbenchV1"
V2_URL = "postgresql://user:secret@db.example.com/MBABenchV2"


def _config_dir(tmp: Path) -> Path:
    (tmp / "config_default.yaml").write_text(
        "database:\n  v1_url: null\n  v2_url: null\n"
        "aws:\n  s3_bucket: mbabench\n  access_key_id: null\n"
        "  secret_access_key: null\n"
        'keys:\n  anthropic_api_key: "${env:ANTHROPIC_API_KEY}"\n'
    )
    (tmp / "config.yaml").write_text(
        f"database:\n  v1_url: {V1_URL}\n  v2_url: {V2_URL}\n"
    )
    os.environ[repo_config.REPO_CONFIG_DIR_ENV] = str(tmp)
    os.environ.pop("DATABASE_URL", None)
    return tmp


def _run_cfg(tmp: Path, text: str):
    p = tmp / "run.yaml"
    p.write_text(text)
    return load_config(p)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # Standalone path: no config dir -> $DATABASE_URL, else unresolved.
        os.environ[repo_config.REPO_CONFIG_DIR_ENV] = str(tmp / "empty")
        os.environ["DATABASE_URL"] = V1_URL
        assert repo_config.resolve_db_url("v1") == (V1_URL, "$DATABASE_URL")
        del os.environ["DATABASE_URL"]
        assert repo_config.resolve_db_url("v1") == ("", "unresolved")
        assert repo_config.database_name(V2_URL) == "MBABenchV2"
        print("OK  env fallback when no repo config")

        if not HAVE_CONFIG_MODULE:
            print("SKIP monorepo `config` module not installed; remaining checks skipped")
            return 0

        cfg_dir = _config_dir(tmp)
        assert repo_config.resolve_db_url("v1") == (V1_URL, "config/config.yaml database.v1_url")
        assert repo_config.resolve_db_url("v2") == (V2_URL, "config/config.yaml database.v2_url")
        os.environ["DATABASE_URL"] = "postgresql://env@host/Elsewhere"
        assert repo_config.resolve_db_url("v2")[0] == V2_URL
        del os.environ["DATABASE_URL"]
        print("OK  benchmark selects the URL; repo config beats env")

        assert repo_config.describe_database_target("v2") == \
            "MBABenchV2 (from config/config.yaml database.v2_url)"
        assert "secret" not in repo_config.describe_database_target("v2")
        print("OK  describe_database_target hides the password")

        assert repo_config.repo_value("aws", "access_key_id") is None
        assert repo_config.repo_value("aws", "s3_bucket") == "mbabench"
        assert repo_config.boto3_credentials() == {}
        (cfg_dir / "config.yaml").write_text("aws:\n  access_key_id: AKIATEST\n")
        assert repo_config.boto3_credentials() == {}
        (cfg_dir / "config.yaml").write_text(
            "aws:\n  access_key_id: AKIATEST\n  secret_access_key: shhh\n"
        )
        assert repo_config.boto3_credentials() == {
            "aws_access_key_id": "AKIATEST", "aws_secret_access_key": "shhh"}
        print("OK  null placeholders fall through; creds need both keys")

        # resolve_secrets: api key ladder (env, then keys.*) and the DB guard.
        _config_dir(tmp)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        cfg = _run_cfg(tmp, "mode: internal\nbenchmark: v2\n"
                            "agent_model_name: claudecode_anthropic/claude-fable-5-max\n")
        try:
            resolve_secrets(cfg)
            raise AssertionError("missing key should abort")
        except SystemExit as e:
            assert "ANTHROPIC_API_KEY" in str(e)
        (cfg_dir / "config.yaml").write_text(
            f"database:\n  v1_url: {V1_URL}\n  v2_url: {V2_URL}\n"
            "keys:\n  anthropic_api_key: sk-ant-test\n"
        )
        assert resolve_secrets(cfg) == "sk-ant-test"
        assert (cfg.db_url, cfg.db_source) == (V2_URL, "config/config.yaml database.v2_url")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-env"
        assert resolve_secrets(cfg) == "sk-ant-env"
        print("OK  api key: env first, then config/config.yaml keys.*")

        (cfg_dir / "config.yaml").write_text(
            f"database:\n  v1_url: {V1_URL}\n  v2_url: {V1_URL}\n"
        )
        try:
            resolve_secrets(cfg)
            raise AssertionError("v2 config pointing at BizbenchV1 should abort")
        except SystemExit as e:
            assert "MBABenchV2" in str(e) and "BizbenchV1" in str(e)
        print("OK  benchmark/database mismatch refused")

        assert not (tmp / "empty" / "config.yaml").exists()
        print("OK  reading never creates config.yaml")

    print("ALL REPO CONFIG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
