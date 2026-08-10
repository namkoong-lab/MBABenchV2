#!/usr/bin/env python3
"""Live human-readable view of a coding-agent transcript.

Usage:
    python3 tools/watch_transcript.py <attempt_dir or transcript.jsonl> [--follow]

Renders claude stream-json / codex --json events as they are written:
thinking, text, tool calls, tool results, per-turn usage, final result.
"""
import json
import sys
import time
from pathlib import Path

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, YELLOW, GREEN, MAGENTA, RED = "\033[36m", "\033[33m", "\033[32m", "\033[35m", "\033[31m"


def clip(s, n=400):
    s = str(s).replace("\n", "\n    ")
    return s if len(s) <= n else s[:n] + f" {DIM}[+{len(s)-n} chars]{RESET}"


def render(obj):
    t = obj.get("type")
    # --- claude stream-json ---
    if t == "system" and obj.get("subtype") == "init":
        print(f"{BOLD}== session start :: model={obj.get('model')} "
              f"keySource={obj.get('apiKeySource')} =={RESET}")
    elif t == "assistant":
        for b in (obj.get("message") or {}).get("content") or []:
            k = b.get("type")
            if k == "thinking":
                print(f"{MAGENTA}[thinking]{RESET} {clip(b.get('thinking',''), 500)}")
            elif k == "text" and b.get("text", "").strip():
                print(f"{GREEN}[agent]{RESET} {clip(b['text'], 600)}")
            elif k == "tool_use":
                print(f"{CYAN}[tool -> {b.get('name')}]{RESET} {clip(json.dumps(b.get('input')), 300)}")
        u = (obj.get("message") or {}).get("usage") or {}
        if u.get("output_tokens"):
            print(f"{DIM}    tokens: in={u.get('input_tokens')} out={u.get('output_tokens')} "
                  f"cache_read={u.get('cache_read_input_tokens')}{RESET}")
    elif t == "user":
        for b in (obj.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_result":
                print(f"{YELLOW}[result]{RESET} {clip(b.get('content'), 300)}")
    elif t == "result":
        print(f"{BOLD}== done :: turns={obj.get('num_turns')} "
              f"cost=${obj.get('total_cost_usd')} error={obj.get('is_error')} =={RESET}")
    # --- codex --json ---
    elif t == "item.completed":
        it = obj.get("item") or {}
        k = it.get("type")
        if k == "agent_message":
            print(f"{GREEN}[agent]{RESET} {clip(it.get('text',''), 600)}")
        elif k == "reasoning":
            print(f"{MAGENTA}[thinking]{RESET} {clip(it.get('text',''), 500)}")
        elif k == "command_execution":
            print(f"{CYAN}[command exit={it.get('exit_code')}]{RESET} {clip(it.get('command'), 300)}")
            if it.get("aggregated_output"):
                print(f"{YELLOW}[output]{RESET} {clip(it['aggregated_output'], 300)}")
        elif k == "file_change":
            print(f"{CYAN}[files]{RESET} {clip(json.dumps(it.get('changes')), 300)}")
        elif k == "todo_list":
            items = it.get("items") or []
            done = sum(1 for x in items if x.get("completed"))
            print(f"{DIM}[plan {done}/{len(items)}]{RESET} " +
                  "; ".join(x.get("text", "") for x in items)[:300])
    elif t == "turn.completed":
        u = obj.get("usage") or {}
        print(f"{DIM}    turn tokens: in={u.get('input_tokens')} out={u.get('output_tokens')} "
              f"reasoning={u.get('reasoning_output_tokens')} cached={u.get('cached_input_tokens')}{RESET}")
    elif t == "error":
        print(f"{RED}[error]{RESET} {clip(obj.get('message'), 300)}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    if path.is_dir():
        path = path / "transcript.jsonl"
    follow = "--follow" in sys.argv

    while not path.exists():
        if not follow:
            sys.exit(f"not found: {path}")
        time.sleep(0.5)

    with open(path, errors="replace") as f:
        while True:
            line = f.readline()
            if not line:
                if not follow:
                    break
                time.sleep(0.3)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                render(json.loads(line))
            except json.JSONDecodeError:
                print(f"{DIM}{line[:160]}{RESET}")


if __name__ == "__main__":
    main()
