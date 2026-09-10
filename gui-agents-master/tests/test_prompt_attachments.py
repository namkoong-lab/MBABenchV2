"""Prompt-version attachments: the version selects the files, the runner
uploads them after the task's own files and records their text.

No DB, AWS, or browser access. The house-standards file is read off the
monorepo checkout; the missing-attachment case uses a throwaway registry.

Run from gui-agents-master:  python -m pytest tests/test_prompt_attachments.py
"""
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra.configs import (  # noqa: E402
    PromptVersionError,
    load_configs,
    load_registry,
    resolve_prompt_attachments,
    resolve_prompt_files,
)
from infra.run import (  # noqa: E402
    _write_prompts_file,
    build_engine_config,
    preflight_check,
)

DEFAULT_PATH = REPO / "infra/configs/configs.default.yaml"
NO_OVERRIDES = REPO / "infra/configs/configs.yaml.absent-on-purpose"
HOUSE_STANDARDS = (REPO.parent / "house_standards" / "House_Standards_v1.md").resolve()


def _cfg(prompt_version, **extra):
    return load_configs(
        default_path=DEFAULT_PATH,
        override_path=NO_OVERRIDES,
        run_config_data={
            "benchmark": "v2",
            "prompt_version": prompt_version,
            "provider": {"kind": "claude"},
            "claude_web": {"model": "fable_5", "project_id": "test-project"},
            "source": {"kind": "yaml"},
            "sink": {"kind": "local"},
            **extra,
        },
    )


def _spec(upload_files):
    return NS(
        task_name="TestTask",
        task_id=7,
        upload_files=[Path(p) for p in upload_files],
        solution_name=None,
        metadata={"task_source": "jp", "db_task_id": 7},
        overrides={},
    )


# --- registry ---------------------------------------------------------------


@pytest.mark.parametrize(
    "version, n_files",
    [(204, 3), (205, 1)],
)
def test_house_standards_versions_resolve(version, n_files):
    cfg = _cfg(version)
    files = resolve_prompt_files(cfg)
    assert len(files) == n_files, files
    for f in files:
        assert (REPO / f).is_file(), f
    attachments = resolve_prompt_attachments(cfg)
    assert attachments == [HOUSE_STANDARDS], attachments
    assert all(p.is_absolute() for p in attachments)


def test_pre_attachment_versions_attach_nothing():
    """202/203 predate the house standards; their rows must stay comparable."""
    for version in (0, 9, 200, 201, 202, 203):
        assert resolve_prompt_attachments(_cfg(version)) == [], version
    reg = load_registry()
    assert reg[204].attachments == reg[205].attachments == (
        "../house_standards/House_Standards_v1.md",
    )


def test_missing_attachment_refuses(tmp_path):
    """A version that promises a file it cannot deliver must not run."""
    # resolve_prompt_attachments anchors `..` paths at registry_path's
    # third parent (the repo root for tasks_configs/prompts/registry.yaml),
    # so mirror that depth.
    reg_path = tmp_path / "tasks_configs" / "prompts" / "registry.yaml"
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text(
        yaml.safe_dump(
            {
                "versions": {
                    900: {
                        "label": "ghost attachment",
                        "files": ["tasks_configs/prompts/v000_test.txt"],
                        "attachments": ["../house_standards/Does_Not_Exist.md"],
                    }
                }
            }
        )
    )
    cfg = NS(prompt_version=900, agent=NS(prompt_version=None))
    with pytest.raises(PromptVersionError, match="Does_Not_Exist.md"):
        resolve_prompt_attachments(cfg, registry_path=reg_path)


# --- runner -----------------------------------------------------------------


def test_engine_config_appends_attachment_after_starting_files(tmp_path):
    workbook = tmp_path / "Case.xlsx"
    workbook.write_bytes(b"PK\x03\x04")
    case_pdf = tmp_path / "Case.pdf"
    case_pdf.write_bytes(b"%PDF")
    spec = _spec([workbook, case_pdf])
    cfg = _cfg(204)

    ec = build_engine_config(cfg, spec, "claude_fable_5_max")

    assert ec["upload_files"] == [str(workbook), str(case_pdf), str(HOUSE_STANDARDS)]
    assert ec["prompt_attachments"] == [str(HOUSE_STANDARDS)]
    # The source's record of the task's own files is untouched.
    assert spec.upload_files == [workbook, case_pdf]
    assert not preflight_check(ec, "claude", "v2")


def test_engine_config_without_attachments_has_no_key():
    ec = build_engine_config(_cfg(202), _spec([]), "claude_fable_5_max")
    assert ec["upload_files"] == []
    assert "prompt_attachments" not in ec


def test_preflight_names_a_missing_attachment():
    ec = build_engine_config(_cfg(204), _spec([]), "claude_fable_5_max")
    ghost = str(HOUSE_STANDARDS.with_name("House_Standards_v0.md"))
    ec["upload_files"] = [ghost]
    ec["prompt_attachments"] = [ghost]
    errors = preflight_check(ec, "claude", "v2")
    assert any("prompt_version=204 attachment not found" in e for e in errors), errors


def test_preflight_catches_attachment_dropped_from_uploads():
    ec = build_engine_config(_cfg(204), _spec([]), "claude_fable_5_max")
    ec["upload_files"] = []
    errors = preflight_check(ec, "claude", "v2")
    assert any("not in upload_files" in e for e in errors), errors


def test_prompts_json_records_attachment_text(tmp_path):
    ec = build_engine_config(_cfg(205), _spec([]), "claude_fable_5_max")
    started = datetime(2026, 9, 10, 12, 0, 0)
    path = _write_prompts_file(tmp_path, "TestTask", ec, started)
    assert path is not None and path.exists()
    payload = json.loads(path.read_text())

    assert payload["prompt_version"] == 205
    assert len(payload["prompts"]) == 1
    (att,) = payload["attachments"]
    raw = HOUSE_STANDARDS.read_bytes()
    assert att["name"] == "House_Standards_v1.md"
    assert att["path"] == str(HOUSE_STANDARDS)
    assert att["sha256"] == hashlib.sha256(raw).hexdigest()
    assert att["text"] == raw.decode("utf-8")
    # The prompt tells the agent to read the file by this exact name.
    assert "House_Standards_v1.md" in payload["prompts"][0]


def test_prompts_json_without_attachments_is_empty_list(tmp_path):
    ec = build_engine_config(_cfg(203), _spec([]), "claude_fable_5_max")
    path = _write_prompts_file(tmp_path, "TestTask", ec, datetime(2026, 9, 10))
    assert json.loads(path.read_text())["attachments"] == []
