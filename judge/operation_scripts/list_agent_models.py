"""
List all unique agent_model_name values from the task_attempts table.

Usage:
    python judge/operation_scripts/list_agent_models.py
"""

import os
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.misc_utils import load_project_configs

load_project_configs()


def get_db_connection():
    db_url = os.environ.get("BIZBENCHJUDGE_KEYS_DATABASE_URL")
    if not db_url:
        print("Error: BIZBENCHJUDGE_KEYS_DATABASE_URL not set.")
        sys.exit(1)
    return psycopg2.connect(db_url)


def main():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT agent_model_name, prompt_version, COUNT(*) as attempt_count
                FROM task_attempts
                GROUP BY agent_model_name, prompt_version
                ORDER BY agent_model_name, prompt_version
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("No attempts found.")
        return

    print(f"{'Agent Model Name':<50} {'Prompt Ver':>12} {'Attempts':>10}")
    print("=" * 74)
    current_model = None
    model_total = 0
    for model_name, prompt_version, count in rows:
        if current_model is not None and model_name != current_model:
            print(f"{'':>50} {'subtotal':>12} {model_total:>10}")
            print("-" * 74)
            model_total = 0
        current_model = model_name
        model_total += count
        pv = str(prompt_version) if prompt_version is not None else "NULL"
        print(f"{model_name:<50} {pv:>12} {count:>10}")
    if current_model is not None:
        print(f"{'':>50} {'subtotal':>12} {model_total:>10}")

    unique_models = len(set(r[0] for r in rows))
    print(f"\nTotal unique models: {unique_models}")


if __name__ == "__main__":
    main()
