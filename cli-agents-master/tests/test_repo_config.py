"""Offline tests for the monorepo-config resolution ladder.

Everything runs against a throwaway config directory selected via
MBABENCH_CONFIG_DIR — no real config/config.yaml, database, or AWS is
touched. Tests that need the shared `config` module (installed by the
MBABenchV2 workspace) skip cleanly where it is absent (e.g. standalone CI).
"""

import importlib.util

import pytest

from excel_cli_agent import repo_config
from excel_cli_agent.db import database

HAVE_CONFIG_MODULE = importlib.util.find_spec("config") is not None

needs_config_module = pytest.mark.skipif(
    not HAVE_CONFIG_MODULE, reason="monorepo `config` module not installed"
)

V1_URL = "postgresql://user:secret@db.example.com/BizbenchV1"
V2_URL = "postgresql://user:secret@db.example.com/MBABenchV2"


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A minimal monorepo config directory, wired up via MBABENCH_CONFIG_DIR."""
    (tmp_path / "config_default.yaml").write_text(
        "database:\n  v1_url: null\n  v2_url: null\n"
        "aws:\n  s3_bucket: mbabench\n  access_key_id: null\n"
        "  secret_access_key: null\n"
    )
    (tmp_path / "config.yaml").write_text(
        f"database:\n  v1_url: {V1_URL}\n  v2_url: {V2_URL}\n"
    )
    monkeypatch.setenv(repo_config.REPO_CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return tmp_path


@needs_config_module
def test_benchmark_selects_url(config_dir):
    assert repo_config.resolve_db_url("v1") == (
        V1_URL, "config/config.yaml database.v1_url"
    )
    assert repo_config.resolve_db_url("v2") == (
        V2_URL, "config/config.yaml database.v2_url"
    )


@needs_config_module
def test_repo_config_beats_env(config_dir, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env@host/Elsewhere")
    url, source = repo_config.resolve_db_url("v2")
    assert url == V2_URL
    assert source == "config/config.yaml database.v2_url"


def test_env_fallback_when_no_repo_config(tmp_path, monkeypatch):
    # Empty directory: Config.load fails (no config_default.yaml), so the
    # ladder must fall through to $DATABASE_URL — the standalone path.
    monkeypatch.setenv(repo_config.REPO_CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", V1_URL)
    assert repo_config.resolve_db_url("v1") == (V1_URL, "$DATABASE_URL")

    monkeypatch.delenv("DATABASE_URL")
    assert repo_config.resolve_db_url("v1") == ("", "unresolved")


@needs_config_module
def test_null_placeholder_falls_through(config_dir):
    # config.yaml doesn't set aws.*; the defaults ship null placeholders,
    # which must read as absent, not as an empty credential.
    assert repo_config.repo_value("aws", "access_key_id") is None
    assert repo_config.repo_value("aws", "s3_bucket") == "mbabench"
    assert repo_config.boto3_credentials() == {}


@needs_config_module
def test_creds_require_both_keys(config_dir):
    (config_dir / "config.yaml").write_text(
        "aws:\n  access_key_id: AKIATEST\n"
    )
    assert repo_config.boto3_credentials() == {}

    (config_dir / "config.yaml").write_text(
        "aws:\n  access_key_id: AKIATEST\n  secret_access_key: shhh\n"
    )
    assert repo_config.boto3_credentials() == {
        "aws_access_key_id": "AKIATEST",
        "aws_secret_access_key": "shhh",
    }


@needs_config_module
def test_api_keys_from_repo_config(config_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (config_dir / "config_default.yaml").write_text(
        'keys:\n  anthropic_api_key: "${env:ANTHROPIC_API_KEY}"\n'
        '  openai_api_key: "${env:OPENAI_API_KEY}"\n'
    )
    # Unset ${env:VAR} expands to "" — must read as absent, not a "" key.
    (config_dir / "config.yaml").write_text("keys: {}\n")
    assert repo_config.repo_value("keys", "anthropic_api_key") is None

    # A literal value in config.yaml overrides the env reference.
    (config_dir / "config.yaml").write_text(
        "keys:\n  anthropic_api_key: sk-ant-test123\n"
    )
    assert repo_config.repo_value("keys", "anthropic_api_key") == "sk-ant-test123"
    assert repo_config.repo_value("keys", "openai_api_key") is None


@needs_config_module
def test_describe_target_hides_password(config_dir):
    described = repo_config.describe_database_target("v2")
    assert described == "MBABenchV2 (from config/config.yaml database.v2_url)"
    assert "secret" not in described


@needs_config_module
def test_reading_never_creates_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "config_default.yaml").write_text("database:\n  v1_url: null\n")
    monkeypatch.setenv(repo_config.REPO_CONFIG_DIR_ENV, str(tmp_path))
    repo_config.repo_value("database", "v1_url")
    assert not (tmp_path / "config.yaml").exists()


def test_configure_refuses_benchmark_switch_after_connect(monkeypatch):
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_benchmark", None)
    database.configure("v1")
    database.configure("v1")  # idempotent re-configure is fine
    monkeypatch.setattr(database, "_engine", object())  # pretend connected
    database.configure("v1")  # same benchmark still fine
    with pytest.raises(RuntimeError, match="already connected"):
        database.configure("v2")
