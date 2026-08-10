"""
Fuzzy search for tasks by name using a cheap LLM via OpenRouter.

Usage:
    source judge/project_configs.sh
    python judge/operation_scripts/search_tasks.py "some task description"
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from openai import OpenAI

# Add judge/ to path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.misc_utils import load_project_configs
from utils.llm_utils import robust_send_message

# Load configs into env vars
load_project_configs()


def get_db_connection():
    db_url = os.environ.get("BIZBENCHJUDGE_KEYS_DATABASE_URL")
    if not db_url:
        print("Error: BIZBENCHJUDGE_KEYS_DATABASE_URL not set.")
        sys.exit(1)
    return psycopg2.connect(db_url)


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


def fuzzy_search_with_llm(query, tasks, api_key, top_k=10):
    """Use a cheap LLM via OpenRouter to rank tasks by relevance to the query."""
    task_list = "\n".join(
        f"  ID={t['id']} | name={t['task_name']} | source={t['task_source']} | deprecated={t['deprecated']}"
        for t in tasks
    )

    prompt = f"""You are a fuzzy search helper. Given a search query and a list of tasks, return the IDs of the top {top_k} most relevant tasks ranked by similarity to the query.

Search query: "{query}"

Tasks:
{task_list}

Return ONLY a JSON array of task IDs in order of relevance, e.g. [3, 17, 5]. No explanation."""

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )
    messages = [{"role": "user", "content": prompt}]
    response, _ = robust_send_message(client, messages, model="google/gemini-2.5-flash-lite")

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
    parser.add_argument("query", type=str, help="Search query for task name")
    parser.add_argument("--top-k", type=int, default=10, help="Number of results to return (default: 10)")
    args = parser.parse_args()

    api_key = os.environ.get("BIZBENCHJUDGE_KEYS_OPEN_ROUTER_API_KEY")
    if not api_key:
        print("Error: BIZBENCHJUDGE_KEYS_OPEN_ROUTER_API_KEY not set.")
        sys.exit(1)

    conn = get_db_connection()
    try:
        tasks = get_all_tasks(conn)
    finally:
        conn.close()

    if not tasks:
        print("No tasks found in database.")
        return

    print(f"Searching {len(tasks)} tasks for: \"{args.query}\"")
    matched_ids = fuzzy_search_with_llm(args.query, tasks, api_key, top_k=args.top_k)

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
