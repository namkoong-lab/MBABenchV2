#!/usr/bin/env python3
"""example.py — minimal demo of the Python two-tiered config loader."""

from config import Config

# Load from the repo root (creates config.yaml from defaults on first run).
cfg = Config.load()

print("app.name      =", cfg["app.name"])          # dotted item access
print("app.log_level =", cfg.get("app.log_level"))
print("app.workers   =", cfg["app"]["workers"])    # dict-style access still works
print("paths.cache   =", cfg.get("paths.cache_dir"))   # ${var} left literal
print("db.password   =", cfg.get("database.password"))  # ${env:...} expanded

print("features:")
for item in cfg.get("features", default=[]):
    print("  -", item)

# require() fails loudly for values you can't run without:
print("db.host       =", cfg.require("database.host"))

# set() layers a runtime override (e.g. a CLI flag) on top of the files:
cfg.set("app.workers", 8)
print("app.workers'  =", cfg["app.workers"], "(overridden in memory)")

print("\nall keys:")
for key in cfg.flat_keys():
    print("  ", key)
