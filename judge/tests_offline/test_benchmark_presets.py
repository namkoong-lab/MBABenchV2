"""Offline check: --benchmark wiring (utils.misc_utils.BENCHMARKS) is
self-consistent and load_project_configs(benchmark=...) pins the right
rubric pair, check_order, S3 root and database name.

Run from judge/:  python tests_offline/test_benchmark_presets.py
No DB, S3, or LLM access. Points the monorepo-config lookup at a temp dir
so the machine's real config/config.yaml is never read.
"""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))

# A fake <repo>/config with one URL per benchmark, so resolve_db_url is
# exercised end to end without touching the real secrets.
_cfg_dir = tempfile.mkdtemp()
Path(_cfg_dir, "config_default.yaml").write_text(
    "database:\n"
    "  v1_url: postgresql://u:p@host/BizbenchV1?sslmode=require\n"
    "  v2_url: postgresql://u:p@host/MBABenchV2?sslmode=require\n"
    "aws:\n  s3_bucket: testbucket\n  access_key_id: null\n  secret_access_key: null\n"
    "keys:\n  openrouter_api_key: null\n"
)
os.environ["MBABENCH_CONFIG_DIR"] = _cfg_dir
logging.getLogger("utils.repo_config").setLevel(logging.ERROR)  # step 6 removes the config on purpose
os.environ.pop("DATABASE_URL", None)

from utils import misc_utils, repo_config  # noqa: E402
from utils.misc_utils import BENCHMARKS, get_db_url, load_project_configs  # noqa: E402

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL:", msg)


def main() -> int:
    # 1. Every benchmark's rubric file exists and its categories == check_order.
    for bm, spec in BENCHMARKS.items():
        rubric = JUDGE / spec["rubric"]
        weights = JUDGE / spec["rubric_weight"]
        check(rubric.is_file(), f"{bm}: rubric missing {rubric}")
        check(weights.is_file(), f"{bm}: weights missing {weights}")
        cats = set(json.loads(rubric.read_text()).keys()) - {"CategoryWeights"}
        order = [c.strip() for c in spec["check_order"].split(",")]
        check(set(order) == cats, f"{bm}: check_order {sorted(order)} != rubric {sorted(cats)}")
        check(len(order) == len(set(order)), f"{bm}: duplicate category in check_order")

    # 2. v1 and v2 disagree on everything benchmark-specific.
    for key in ("db_name", "s3_root", "rubric", "rubric_version", "check_order"):
        check(BENCHMARKS["v1"][key] != BENCHMARKS["v2"][key], f"v1/v2 share {key}")

    # 3. Agnostic load sets no benchmark; no DB URL resolves without one.
    loaded, prefix = load_project_configs()
    check(prefix == "BIZBENCHJUDGE", f"prefix {prefix}")
    check(f"{prefix}_JUDGE_RUBRIC" not in loaded, "agnostic load leaked a rubric")
    check(f"{prefix}_KEYS_DATABASE_URL" not in loaded, "project_configs.yaml still carries keys")
    check(Path(loaded[f"{prefix}_PATHS_SCRATCH_PATH"]).is_absolute(), "scratch_path not absolute")
    try:
        get_db_url()
        check(False, "get_db_url() without a benchmark should raise")
    except EnvironmentError:
        pass

    # 4. Selecting v2 pins the v2 wiring and resolves the v2 database.
    loaded, _ = load_project_configs(benchmark="v2")
    check(loaded[f"{prefix}_JUDGE_RUBRIC_VERSION"] == "9", "v2 rubric_version")
    check(loaded[f"{prefix}_S3_RAW_FILES_PREFIX"] == "MBABenchV2/grading", "v2 s3 prefix")
    check(loaded[f"{prefix}_S3_RAW_FILES_BUCKET"] == "testbucket", "bucket from config/config.yaml")
    check(os.environ[f"{prefix}_JUDGE_CHECK_ORDER"].startswith("Accuracy,Assumptions"), "v2 env")
    check(repo_config.database_name(get_db_url()) == "MBABenchV2", "v2 db url")
    check("p@" not in repo_config.describe_database_target("v2"), "describe leaks password")

    # 5. A process cannot silently switch benchmarks.
    try:
        load_project_configs(benchmark="v1")
        check(False, "switching v2 -> v1 in one process should raise")
    except ValueError:
        pass

    # 6. A URL naming the other database is refused (unless the guard is skipped).
    misc_utils._BENCHMARK = None
    os.environ["DATABASE_URL"] = "postgresql://u:p@host/BizbenchV1"
    os.environ["MBABENCH_CONFIG_DIR"] = tempfile.mkdtemp()  # no config -> env fallback
    load_project_configs(benchmark="v2")
    try:
        get_db_url()
        check(False, "v2 with a BizbenchV1 URL should raise")
    except EnvironmentError:
        pass
    os.environ["JUDGE_SKIP_BENCHMARK_GUARD"] = "1"
    check(repo_config.database_name(get_db_url()) == "BizbenchV1", "guard bypass")

    print("OK" if not FAILS else f"{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
