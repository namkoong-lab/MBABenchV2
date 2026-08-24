import argparse
import os
from pathlib import Path

import yaml

try:
    from . import repo_config
except ImportError:  # imported as a bare module (utils/ on sys.path)
    import repo_config

# judge/ root; project_configs.yaml (benchmark-agnostic, tracked) lives here.
JUDGE_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = JUDGE_ROOT / "project_configs.yaml"

# Per-benchmark wiring. `--benchmark v1|v2` selects everything that differs
# between the two benchmarks: the DB (config/config.yaml database.{v1,v2}_url),
# the S3 grading root under aws.s3_bucket, and the rubric pair + category
# order. Nothing else in the judge is benchmark-specific.
BENCHMARKS = {
    "v1": {
        "db_name": "BizbenchV1",
        "s3_root": "BizbenchV1",
        "rubric": "prompts/rubrics/rubric_8.json",
        "rubric_version": "8",
        "rubric_weight": "prompts/rubrics/rubric_6_weights.json",
        "rubric_weight_version": "6",
        "check_order": "Accuracy,Formula,Formatting",
        # The standard judge template hardcodes one stage per v1 category.
        "agentic_required": False,
    },
    "v2": {
        "db_name": "MBABenchV2",
        "s3_root": "MBABenchV2",
        "rubric": "prompts/rubrics/rubric_9.json",
        "rubric_version": "9",
        "rubric_weight": "prompts/rubrics/rubric_9_weights.json",
        "rubric_weight_version": "9",
        "check_order": (
            "Accuracy,Assumptions,Documentation,Error Checks,Flexibility,"
            "Formatting,Formulas,Model Outputs & Executive Summary,"
            "Potential Dangers,Purpose & Scope,Rounding,Structure"
        ),
        "agentic_required": True,
    },
}

# The benchmark this process is grading, set by load_project_configs(benchmark=...).
_BENCHMARK = None


def get_absolute_path(path) -> str:
    """Convert a path to an absolute path."""
    return str(Path(path).resolve())


def relative_path_from_project_root(path) -> str:
    """Convert a path to be relative from the project root directory."""
    project_root = Path(__file__).parent.parent.resolve()
    path = Path(path)

    # interpret .. or . as relative to project root and resolve to absolute path
    absolute_path = (project_root / path).resolve()
    return absolute_path


def _flatten_dict(d, prefix=""):
    """Recursively flatten a nested dict into {PREFIX_KEY: value} pairs, skipping None values."""
    items = {}
    for key, value in d.items():
        full_key = f"{prefix}_{key.upper()}" if prefix else key.upper()
        if isinstance(value, dict):
            items.update(_flatten_dict(value, full_key))
        elif value is not None:
            items[full_key] = value
    return items


def _read_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


_PREFIX = None


def project_prefix() -> str:
    """Env-var prefix, project.name upper-cased (e.g. BIZBENCHJUDGE)."""
    global _PREFIX
    if _PREFIX is None:
        _PREFIX = _read_config().get("project", {}).get("name", "").upper()
    return _PREFIX


def current_benchmark(required=True):
    """The benchmark selected for this process, or None (raises if required)."""
    if _BENCHMARK is None and required:
        raise EnvironmentError(
            "No benchmark selected: pass --benchmark {v1,v2} (or call "
            "load_project_configs(benchmark=...))."
        )
    return _BENCHMARK


def add_benchmark_arg(parser, required=True):
    """The shared --benchmark flag; every judge CLI that touches a DB, S3 or a rubric takes it."""
    parser.add_argument(
        "--benchmark",
        choices=tuple(BENCHMARKS),
        required=required,
        help=(
            "Which benchmark to grade: selects the database "
            "(config/config.yaml database.<bm>_url), the S3 grading root and "
            "the rubric pair. v2 must be graded with --agentic."
        ),
    )


def get_db_url(benchmark=None) -> str:
    """Postgres URL for the selected benchmark (config/config.yaml first, then $DATABASE_URL)."""
    benchmark = benchmark or current_benchmark()
    url, source = repo_config.resolve_db_url(benchmark)
    if not url:
        raise EnvironmentError(
            f"No database URL for benchmark={benchmark}: set "
            f"database.{benchmark}_url in <MBABenchV2>/config/config.yaml "
            f"(or export DATABASE_URL)."
        )
    expected = BENCHMARKS[benchmark]["db_name"]
    actual = repo_config.database_name(url)
    if actual != expected and os.environ.get("JUDGE_SKIP_BENCHMARK_GUARD") != "1":
        raise EnvironmentError(
            f"benchmark={benchmark} expects the {expected} database, but the "
            f"URL from {source} names {actual!r}. Fix the URL or the benchmark "
            f"(JUDGE_SKIP_BENCHMARK_GUARD=1 bypasses)."
        )
    return url


def _benchmark_env(benchmark, prefix):
    """The {PREFIX}_* vars a benchmark pins (rubric pair, check order, S3 grading root)."""
    bm = BENCHMARKS[benchmark]
    return {
        f"{prefix}_BENCHMARK": benchmark,
        f"{prefix}_JUDGE_RUBRIC": bm["rubric"],
        f"{prefix}_JUDGE_RUBRIC_VERSION": bm["rubric_version"],
        f"{prefix}_JUDGE_RUBRIC_WEIGHT": bm["rubric_weight"],
        f"{prefix}_JUDGE_RUBRIC_WEIGHT_VERSION": bm["rubric_weight_version"],
        f"{prefix}_JUDGE_CHECK_ORDER": bm["check_order"],
        f"{prefix}_S3_RAW_FILES_BUCKET": repo_config.s3_bucket(),
        f"{prefix}_S3_RAW_FILES_PREFIX": f"{bm['s3_root']}/grading",
    }


def load_project_configs(verbose=False, benchmark=None):
    """Load project_configs.yaml (+ the benchmark's wiring) into environment variables.

    Env var names follow: {PROJECT_NAME}_{SECTION}_{...}_{KEY}
    where PROJECT_NAME comes from project.name in the config.

    `benchmark` ("v1" | "v2") additionally sets the rubric/check-order/S3
    vars from BENCHMARKS and records the selection for get_db_url(). Without
    it only the benchmark-agnostic keys are loaded — enough for import-time
    constants; every CLI passes args.benchmark from main().
    """
    global _BENCHMARK
    if verbose:
        print("*" * 126)
        print(f"Loading project configs from {_CONFIG_PATH} (benchmark={benchmark})...")
    config = _read_config()
    prefix = project_prefix()

    loaded_configs = {}
    for section_name, section in config.items():
        if not isinstance(section, dict):
            continue
        section_prefix = f"{prefix}_{section_name.upper()}"
        for env_key, value in _flatten_dict(section, section_prefix).items():
            if section_name == "paths":
                # Relative paths are relative to judge/, whatever the cwd.
                value = str((JUDGE_ROOT / str(value)).resolve())
            loaded_configs[env_key] = value

    if benchmark is not None:
        if benchmark not in BENCHMARKS:
            raise ValueError(f"benchmark must be one of {sorted(BENCHMARKS)}, got {benchmark!r}")
        if _BENCHMARK is not None and _BENCHMARK != benchmark:
            raise ValueError(
                f"benchmark already set to {_BENCHMARK!r} in this process; cannot switch to {benchmark!r}"
            )
        _BENCHMARK = benchmark
        loaded_configs.update(_benchmark_env(benchmark, prefix))

    for env_key, value in loaded_configs.items():
        if verbose:
            print(f"Setting env var {env_key} = {value}")
        os.environ[env_key] = str(value)
    if verbose:
        if benchmark is not None:
            print(f"Database: {repo_config.describe_database_target(benchmark)}")
        print("*" * 126)
    return loaded_configs, prefix


def load_env_var(var_name: str, default=None, prefix=None, required=False):
    """Helper to load an env var with optional default."""
    if prefix is None:
        prefix = project_prefix()
    var_name = f"{prefix}_{var_name.upper()}"
    value = os.environ.get(var_name, None)

    if value is None:
        if not required:
            from .logger import logger

            logger.debug(
                f"Environment variable {var_name} not set. Using default: {default}"
            )
            value = default
        else:
            raise EnvironmentError(f"Required environment variable {var_name} not set.")
    return value


### YAML dump helpers for conversation messages
class _LiteralStr(str):
    pass


def _literal_str_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(_LiteralStr, _literal_str_representer)
yaml.add_representer(_LiteralStr, _literal_str_representer, Dumper=yaml.SafeDumper)


def _blockify_multiline_strings(obj):
    if isinstance(obj, str):
        if "\n" in obj:
            # PyYAML silently falls back to quoted style if any line has trailing whitespace.
            return _LiteralStr("\n".join(line.rstrip() for line in obj.split("\n")))
        return obj
    if isinstance(obj, list):
        return [_blockify_multiline_strings(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _blockify_multiline_strings(v) for k, v in obj.items()}
    return obj


def dump_messages_yaml(messages, path):
    """Write conversation messages as YAML, using literal block scalars for multi-line strings."""
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            _blockify_multiline_strings(messages),
            f,
            sort_keys=False,
            allow_unicode=True,
            width=10_000,
            default_flow_style=False,
        )


### Argparser helper
def str2bool(v):
    """Convert string to boolean for argparse."""
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


if __name__ == "__main__":
    load_project_configs()
