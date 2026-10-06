#!/usr/bin/env python3
"""example.py — minimal demo of the Python two-tiered config loader, against
this repository's own config (config_default.yaml + the gitignored config.yaml).

    python config/python/example.py
"""

from config import Config

# Load from the repo root (creates config.yaml from the defaults on first run).
cfg = Config.load()

print("aws.s3_bucket     =", cfg["aws.s3_bucket"])              # dotted item access
print("venv_path         =", cfg.get("venv_path"))              # null -> None
print("libreoffice_path  =", cfg.get("libreoffice_path"))
print("database.v2_url   =", "set" if cfg.get("database.v2_url") else "unset")   # ${env:V2_DATABASE_URL}
print("keys.openai       =", "set" if cfg.get("keys.openai_api_key") else "unset")

# set() layers a runtime override (e.g. a CLI flag) on top of the files:
cfg.set("venv_path", "/tmp/demo-venv")
print("venv_path'        =", cfg["venv_path"], "(overridden in memory)")

print("\nall keys:")
for key in cfg.flat_keys():
    print("  ", key)
