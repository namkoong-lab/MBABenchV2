#!/usr/bin/env python3
"""Self-contained tests for the Python two-tiered config loader.

No third-party test runner — just run it:

    python python/test_config.py

Each test uses a temp dir, so the repo's real config.yaml is never touched.
"""

import os
import shutil
import tempfile
from pathlib import Path

from config import Config, deep_merge, get, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = REPO_ROOT / "config_default.yaml"

_passed = 0
_failed = 0


def check(label, expected, actual):
    global _passed, _failed
    if expected == actual:
        print(f"  ok: {label}")
        _passed += 1
    else:
        print(f"  FAIL: {label} — expected {expected!r}, got {actual!r}")
        _failed += 1


def new_config_dir():
    """A fresh temp config dir seeded with only config_default.yaml."""
    d = Path(tempfile.mkdtemp())
    shutil.copyfile(DEFAULTS, d / "config_default.yaml")
    return d


def test_creates_config_yaml_on_first_run():
    print("Test 1: create config.yaml on first run, defaults resolve")
    d = new_config_dir()
    check("config.yaml absent before load", False, (d / "config.yaml").exists())
    cfg = load_config(d)
    check("config.yaml created", True, (d / "config.yaml").exists())
    check("app.name from defaults", "my-app", get(cfg, "app.name"))
    check("app.workers from defaults", 4, get(cfg, "app.workers"))


def test_partial_override_falls_back():
    print("Test 2: partial override falls back to defaults for omitted keys")
    d = new_config_dir()
    (d / "config.yaml").write_text("app:\n  log_level: debug\n")
    cfg = load_config(d)
    check("overridden log_level", "debug", get(cfg, "app.log_level"))
    check("omitted workers falls back", 4, get(cfg, "app.workers"))
    check("omitted name falls back", "my-app", get(cfg, "app.name"))


def test_nested_fallback_preserves_siblings():
    print("Test 3: nested override preserves sibling defaults")
    d = new_config_dir()
    (d / "config.yaml").write_text("database:\n  host: db.prod\n")
    cfg = load_config(d)
    check("overridden host", "db.prod", get(cfg, "database.host"))
    check("sibling port falls back", 5432, get(cfg, "database.port"))


def test_env_expansion():
    print("Test 4: ${env:VAR:-default} expansion")
    d = new_config_dir()
    saved = os.environ.pop("DB_PASSWORD", None)
    try:
        cfg = load_config(d)
        check("uses env default", "changeme", get(cfg, "database.password"))
        os.environ["DB_PASSWORD"] = "s3cret"
        cfg = load_config(d)
        check("uses env when set", "s3cret", get(cfg, "database.password"))
    finally:
        os.environ.pop("DB_PASSWORD", None)
        if saved is not None:
            os.environ["DB_PASSWORD"] = saved


def test_get_default_for_missing():
    print("Test 5: get() returns default for missing key")
    d = new_config_dir()
    cfg = load_config(d)
    check("missing key default", "fallback", get(cfg, "does.not.exist", default="fallback"))


def test_deep_merge_replaces_lists():
    print("Test 6: deep_merge replaces lists wholesale, merges dicts")
    merged = deep_merge(
        {"a": {"x": 1, "y": 2}, "items": [1, 2, 3]},
        {"a": {"y": 20}, "items": [9]},
    )
    check("deep merge result", {"a": {"x": 1, "y": 20}, "items": [9]}, merged)


def test_config_class_api():
    print("Test 7: Config class — dotted/dict access, contains, require, keys")
    d = new_config_dir()
    cfg = load_config(d)
    check("load_config returns Config", True, isinstance(cfg, Config))
    check("dotted item access", "my-app", cfg["app.name"])
    check("dict-style access still works", 4, cfg["app"]["workers"])
    check("contains dotted key", True, "database.host" in cfg)
    check("contains missing key", False, "database.nope" in cfg)
    check("require present", "localhost", cfg.require("database.host"))
    check("flat_keys includes leaf", True, "database.port" in cfg.flat_keys())


def test_config_require_and_set():
    print("Test 8: Config.require raises; Config.set overrides in memory")
    d = new_config_dir()
    cfg = load_config(d)
    raised = False
    try:
        cfg.require("does.not.exist")
    except KeyError:
        raised = True
    check("require raises on missing", True, raised)
    cfg.set("app.workers", 8)
    check("set overrides existing", 8, cfg["app.workers"])
    cfg.set("new.nested.flag", True)
    check("set creates nested path", True, cfg["new.nested.flag"])


def main():
    for test in (
        test_creates_config_yaml_on_first_run,
        test_partial_override_falls_back,
        test_nested_fallback_preserves_siblings,
        test_env_expansion,
        test_get_default_for_missing,
        test_deep_merge_replaces_lists,
        test_config_class_api,
        test_config_require_and_set,
    ):
        test()

    print(f"\nPassed: {_passed}  Failed: {_failed}")
    raise SystemExit(1 if _failed else 0)


if __name__ == "__main__":
    main()
