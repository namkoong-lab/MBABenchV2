"""
Fuzzy search for tasks by name using a cheap LLM via OpenRouter.

Usage:
    python judge/operation_scripts/search_tasks.py "some task description"
"""

import argparse
import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# Add judge/ to path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.misc_utils import add_benchmark_arg, get_db_url, load_project_configs
from utils.judge_identity import resolve_judge_identity
from utils.llm_utils import get_client, robust_send_message

# Cheap ranking model; resolved through the judge identity registry like any
# other model so routing and keys stay in one place.
SEARCH_MODEL = "google/gemini-2.5-flash-lite"

# Load configs into env vars
load_project_configs()


def get_db_connection():
    return psycopg2.connect(get_db_url())


def get_all_tasks(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, task_name, task_source, deprecated
            FROM tasks
            ORDER BY id
            """
        )
        return cur.fetchall()


def fuzzy_search_with_llm(query, tasks, top_k=10):
    """Use a cheap LLM to rank tasks by relevance to the query."""
    task_list = "\n".join(
        f"  ID={t['id']} | name={t['task_name']} | source={t['task_source']} | deprecated={t['deprecated']}"
        for t in tasks
    )

    prompt = f"""You are a fuzzy search helper. Given a search query and a list of tasks, return the IDs of the top {top_k} most relevant tasks ranked by similarity to the query.

Search query: "{query}"

Tasks:
{task_list}

Return ONLY a JSON array of task IDs in order of relevance, e.g. [3, 17, 5]. No explanation."""

    identity = resolve_judge_identity(SEARCH_MODEL)
    client = get_client(identity)
    messages = [{"role": "user", "content": prompt}]
    response, _ = robust_send_message(client, messages, identity)

    content = response.choices[0].message.content.strip()
    # Extract JSON array from response (handle markdown code blocks)
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    matched_ids = json.loads(content)
    return matched_ids


def main():
    parser = argparse.ArgumentParser(description="Fuzzy search tasks by name using LLM")
    add_benchmark_arg(parser)
    parser.add_argument("query", type=str, help="Search query for task name")
    parser.add_argument("--top-k", type=int, default=10, help="Number of results to return (default: 10)")
    args = parser.parse_args()
    load_project_configs(benchmark=args.benchmark)

    conn = get_db_connection()
    try:
        tasks = get_all_tasks(conn)
    finally:
        conn.close()

    if not tasks:
        print("No tasks found in database.")
        return

    print(f"Searching {len(tasks)} tasks for: \"{args.query}\"")
    matched_ids = fuzzy_search_with_llm(args.query, tasks, top_k=args.top_k)

    # Build lookup and display results
    task_lookup = {t["id"]: t for t in tasks}
    print(f"\nTop {len(matched_ids)} matches:\n")
    print(f"{'Rank':<6}{'ID':<8}{'Source':<15}{'Deprecated':<12}{'Task Name'}")
    print("-" * 80)
    for rank, tid in enumerate(matched_ids, 1):
        t = task_lookup.get(tid)
        if t:
            dep = "Yes" if t["deprecated"] else "No"
            print(f"{rank:<6}{t['id']:<8}{t['task_source'] or '':<15}{dep:<12}{t['task_name']}")


if __name__ == "__main__":
    main()
