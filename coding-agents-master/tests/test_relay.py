"""Offline test of the trajectory relay: fake upstream, no real API, no keys.

Run: python3 tests/test_relay.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        assert self.headers.get("x-api-key"), "auth header should be forwarded upstream"
        if self.path == "/v1/messages":
            out = json.dumps({"id": "msg_1", "content": [{"type": "text", "text": "hi"}],
                              "echo_len": len(body)}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        else:  # SSE stream
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"event: delta\ndata: {{\"n\": {i}}}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)


def main():
    up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp())
    out = tmp / "trajectory.jsonl"
    env = {**os.environ,
           "TRAJ_UPSTREAM": f"http://127.0.0.1:{up.server_port}",
           "TRAJ_PATH": str(out), "TRAJ_PORT": "19877"}
    relay = subprocess.Popen([sys.executable, str(ROOT / "docker" / "traj_relay.py")],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                import socket
                socket.create_connection(("127.0.0.1", 19877), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)

        # JSON round-trip
        req = urllib.request.Request("http://127.0.0.1:19877/v1/messages",
                                     data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "yo"}]}).encode(),
                                     headers={"content-type": "application/json", "x-api-key": "sk-secret"})
        resp = json.loads(urllib.request.urlopen(req).read())
        assert resp["id"] == "msg_1", resp

        # SSE round-trip
        req2 = urllib.request.Request("http://127.0.0.1:19877/v1/stream", data=b"{}",
                                      headers={"content-type": "application/json", "x-api-key": "sk-secret"})
        sse = urllib.request.urlopen(req2).read().decode()
        assert sse.count("event: delta") == 3, sse

        time.sleep(0.3)
        records = [json.loads(l) for l in open(out)]
        assert len(records) == 2, records
        r1, r2 = records
        assert r1["step"] == 1 and r1["status"] == 200
        assert r1["request"]["messages"][0]["content"] == "yo"
        assert r1["request_headers"].get("x-api-key") == "<scrubbed>", "auth must be scrubbed in the record"
        assert r1["response"]["id"] == "msg_1"
        assert r2["response"]["_raw_text"].count("event: delta") == 3, "SSE stored raw"
        print("ok: relay forwards, streams, records, scrubs")
        dead_upstream_gets_a_complete_502(tmp)
        silent_upstream_is_cut_at_the_timeout(tmp)
        request_fixes_change_only_what_is_forwarded(tmp)
        unknown_request_fix_refuses_to_start(tmp)
        chat_wire_translates_both_ways(tmp)
        print("ALL RELAY TESTS PASSED")
    finally:
        relay.terminate()
        up.shutdown()


def dead_upstream_gets_a_complete_502(tmp: Path):
    """The upstream connection fails before any reply: the client must get a
    502 that ENDS (zero length, connection closed). The old bare 502 on a
    keep-alive socket left Claude Code waiting 68 min for a body (2026-09-20)."""
    import http.client
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead_port = s.getsockname()[1]; s.close()
    out = tmp / "dead.jsonl"
    env = {**os.environ, "TRAJ_UPSTREAM": f"http://127.0.0.1:{dead_port}",
           "TRAJ_PATH": str(out), "TRAJ_PORT": "19878"}
    relay = subprocess.Popen([sys.executable, str(ROOT / "docker" / "traj_relay.py")],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                socket.create_connection(("127.0.0.1", 19878), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        conn = http.client.HTTPConnection("127.0.0.1", 19878, timeout=10)  # a hang fails here
        t0 = time.time()
        conn.request("POST", "/v1/messages", body=b"{}",
                     headers={"content-type": "application/json", "x-api-key": "sk-secret"})
        resp = conn.getresponse()
        body = resp.read()  # the old reply never finished this read
        assert resp.status == 502 and body == b"", (resp.status, body)
        assert resp.getheader("Content-Length") == "0" and resp.getheader("Connection") == "close"
        assert time.time() - t0 < 5
        time.sleep(0.3)
        rec = json.loads(open(out).readline())
        assert rec["status"] == -1 and rec["error"]["phase"] == "upstream_open", rec
        print("ok: a dead upstream gets a complete, connection-closing 502")
    finally:
        relay.terminate()


def silent_upstream_is_cut_at_the_timeout(tmp: Path):
    """The upstream accepts the call and never answers (TensorBlock Forge,
    2026-09-20: one call sat 2.6 h). TRAJ_UPSTREAM_TIMEOUT bounds the wait and
    the client gets the same complete 502 as for a dead upstream — Codex has
    no timeout of its own before the reply starts."""
    import http.client
    import socket
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)  # connections are accepted by the kernel, never read
    out = tmp / "silent.jsonl"
    env = {**os.environ, "TRAJ_UPSTREAM": f"http://127.0.0.1:{silent.getsockname()[1]}",
           "TRAJ_UPSTREAM_TIMEOUT": "1", "TRAJ_PATH": str(out), "TRAJ_PORT": "19879"}
    relay = subprocess.Popen([sys.executable, str(ROOT / "docker" / "traj_relay.py")],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                socket.create_connection(("127.0.0.1", 19879), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        conn = http.client.HTTPConnection("127.0.0.1", 19879, timeout=10)  # an unbounded wait fails here
        t0 = time.time()
        conn.request("POST", "/v1/responses", body=b"{}",
                     headers={"content-type": "application/json", "x-api-key": "sk-secret"})
        resp = conn.getresponse()
        body = resp.read()
        waited = time.time() - t0
        assert resp.status == 502 and body == b"", (resp.status, body)
        assert resp.getheader("Content-Length") == "0" and resp.getheader("Connection") == "close"
        assert 0.9 < waited < 5, waited
        time.sleep(0.3)
        rec = json.loads(open(out).readline())
        assert rec["status"] == -1 and rec["error"]["phase"] == "upstream_open", rec
        print("ok: a silent upstream is cut at TRAJ_UPSTREAM_TIMEOUT with a complete 502")
    finally:
        relay.terminate()
        silent.close()


class CapturingUpstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        CapturingUpstream.seen.append(json.loads(self.rfile.read(int(self.headers.get("content-length", 0)))))
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def request_fixes_change_only_what_is_forwarded(tmp: Path):
    """TRAJ_REQUEST_FIXES=reasoning_null_content: the upstream gets the echoed
    reasoning item without "content": null (xAI 400s on it, 2026-09-21); the
    record keeps what the CLI sent and names the fix; a request with nothing
    to fix is forwarded byte for byte."""
    import socket
    up = ThreadingHTTPServer(("127.0.0.1", 0), CapturingUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    out = tmp / "fixes.jsonl"
    env = {**os.environ, "TRAJ_UPSTREAM": f"http://127.0.0.1:{up.server_port}",
           "TRAJ_REQUEST_FIXES": "reasoning_null_content", "TRAJ_PATH": str(out), "TRAJ_PORT": "19880"}
    relay = subprocess.Popen([sys.executable, str(ROOT / "docker" / "traj_relay.py")],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                socket.create_connection(("127.0.0.1", 19880), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        sent = {"model": "m", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "id": "rs_1", "summary": [], "content": None, "encrypted_content": "abc"},
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"}]}
        clean = {"model": "m", "input": [{"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "x"}]}]}
        for body in (sent, clean):
            req = urllib.request.Request("http://127.0.0.1:19880/v1/responses", data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json", "x-api-key": "sk-secret"})
            assert json.loads(urllib.request.urlopen(req, timeout=10).read()) == {"ok": True}
        fixed, untouched = CapturingUpstream.seen
        assert "content" not in fixed["input"][1] and fixed["input"][1]["encrypted_content"] == "abc", fixed
        assert fixed["input"][0] == sent["input"][0] and fixed["input"][2] == sent["input"][2]
        assert untouched == clean, "a request with nothing to fix is not rewritten"
        time.sleep(0.3)
        r1, r2 = [json.loads(l) for l in open(out)]
        assert r1["request"] == sent and r1["request_fixes"] == ["reasoning_null_content"], r1
        assert "request_fixes" not in r2
        print("ok: request fixes rewrite only what is forwarded; the record keeps what was sent")
    finally:
        relay.terminate()
        up.shutdown()


def unknown_request_fix_refuses_to_start(tmp: Path):
    """A misspelt fix name must stop the relay (the entrypoint then aborts the
    attempt as infra, no row) rather than run without the fix."""
    env = {**os.environ, "TRAJ_UPSTREAM": "http://127.0.0.1:9", "TRAJ_REQUEST_FIXES": "reasoning_nul_content",
           "TRAJ_PATH": str(tmp / "unknown.jsonl"), "TRAJ_PORT": "19881"}
    p = subprocess.run([sys.executable, str(ROOT / "docker" / "traj_relay.py")], env=env,
                       capture_output=True, text=True, timeout=20)
    assert p.returncode != 0 and "unknown TRAJ_REQUEST_FIXES" in p.stderr, (p.returncode, p.stderr)
    print("ok: an unknown request fix stops the relay")


class FakeChatUpstream(BaseHTTPRequestHandler):
    """/v1/chat/completions: turn 1 streams thinking, text and a tool call with
    a provider-specific signature; turn 2 a plain answer; model "cut" stops
    at the length limit; model "bad" is refused with a 400."""
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        FakeChatUpstream.seen.append((self.path, body))
        quota_first = body["model"] == "quota-once" and not any(
            b["model"] == "quota-once" for _, b in FakeChatUpstream.seen[:-1])
        if body["model"] == "bad" or quota_first:
            out = (b'{"error": {"message": "The configured provider rate limit, quota, or billing limit was reached.'
                   b' Please check your provider account or try again later.", "type": "provider_error", "code": 400}}'
                   if quota_first else b'{"error": {"message": "rejected", "type": "provider_error", "code": 400}}')
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        tool_turn = not any(m["role"] == "tool" for m in body["messages"])

        def chunk(delta=None, finish=None, usage=None):
            c = {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            if usage:
                c["usage"] = usage
            return f"data: {json.dumps(c)}\n\n".encode()

        if body["model"] == "cut":
            parts = [chunk({"content": "partial"}), chunk(finish="length"),
                     chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})]
        elif body["model"] == "malformed":
            parts = [chunk({"content": "print('x')"}), chunk(finish="function_call_filter: MALFORMED_FUNCTION_CALL"),
                     chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})]
        elif tool_turn:
            parts = [chunk({"role": "assistant", "reasoning_content": "think"}), chunk({"reasoning_content": "ing"}),
                     chunk({"content": "Running it."}),
                     chunk({"tool_calls": [{"index": 0, "id": "call_A", "type": "function",
                                            "function": {"name": "exec_command", "arguments": '{"cmd": '},
                                            "extra_content": {"google": {"thought_signature": "SIG"}}}]}),
                     chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"echo hi"}'}}]}),
                     chunk(finish="tool_calls"),
                     chunk(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 150,
                                  "prompt_tokens_details": {"cached_tokens": 64}})]
        else:
            parts = [chunk({"content": "done"}), chunk(finish="stop"),
                     chunk(usage={"prompt_tokens": 120, "completion_tokens": 2, "total_tokens": 122})]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for p in parts + [b"data: [DONE]\n\n"]:
            self.wfile.write(p)
            self.wfile.flush()


def _sse_events(raw: str):
    events = []
    for block in raw.split("\n\n"):
        data = [l[5:].strip() for l in block.splitlines() if l.startswith("data:")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


def chat_wire_translates_both_ways(tmp: Path):
    """TRAJ_WIRE=chat: a Responses call goes out as a streamed chat call and
    the reply comes back as Responses events; on the next call the assistant
    turn is replayed exactly as the provider sent it (reasoning_content and
    the tool call's extra_content signature included); errors pass through;
    a length stop becomes response.incomplete."""
    import socket
    up = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    out = tmp / "chat.jsonl"
    env = {**os.environ, "TRAJ_UPSTREAM": f"http://127.0.0.1:{up.server_port}", "TRAJ_WIRE": "chat",
           "TRAJ_CHAT_MAX_TOKENS": "65536", "TRAJ_PATH": str(out), "TRAJ_PORT": "19882"}
    relay = subprocess.Popen([sys.executable, str(ROOT / "docker" / "traj_relay.py")],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def post(body):
        req = urllib.request.Request("http://127.0.0.1:19882/v1/responses", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json", "authorization": "Bearer sk-secret"})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    try:
        for _ in range(40):
            try:
                socket.create_connection(("127.0.0.1", 19882), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        dev = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "DEV"}]}
        user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        tools = [{"type": "function", "name": "exec_command", "description": "Run", "strict": False,
                  "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
                 {"type": "web_search", "external_web_access": True}]
        first = {"model": "m", "instructions": "INS", "input": [dev, user], "tools": tools, "tool_choice": "auto",
                 "parallel_tool_calls": True, "reasoning": {"effort": "max", "summary": "auto"}, "stream": True,
                 "store": False, "include": ["reasoning.encrypted_content"]}
        status, raw = post(first)
        assert status == 200, (status, raw)
        events = _sse_events(raw)
        kinds = [e["type"] for e in events]
        assert kinds[0] == "response.created" and kinds[-1] == "response.completed", kinds
        assert "response.reasoning_summary_text.delta" in kinds and "response.output_text.delta" in kinds
        items = [e["item"] for e in events if e["type"] == "response.output_item.done"]
        assert [i["type"] for i in items] == ["reasoning", "message", "function_call"], items
        assert items[0]["summary"][0]["text"] == "thinking" and items[1]["content"][0]["text"] == "Running it."
        assert items[2]["call_id"] == "call_A" and json.loads(items[2]["arguments"]) == {"cmd": "echo hi"}
        usage = events[-1]["response"]["usage"]
        assert usage == {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 64}, "output_tokens": 50,
                         "output_tokens_details": {"reasoning_tokens": 30}, "total_tokens": 150}, usage
        path, sent = FakeChatUpstream.seen[0]
        assert path == "/v1/chat/completions", path
        assert sent["messages"] == [{"role": "system", "content": "INS\n\nDEV"}, {"role": "user", "content": "hi"}]
        assert [t["function"]["name"] for t in sent["tools"]] == ["exec_command"], "non-function tools are dropped"
        assert sent["reasoning_effort"] == "max" and sent["max_completion_tokens"] == 65536 and sent["stream"]
        assert "store" not in sent and "include" not in sent and "thinking" not in sent

        # Codex's next call replays the turn from the items it was given.
        echoed = [dict(items[0], content=None), items[1], items[2],
                  {"type": "function_call_output", "call_id": "call_A", "output": "hi\n"}]
        status, raw = post(dict(first, input=[dev, user] + echoed))
        assert status == 200 and _sse_events(raw)[-1]["type"] == "response.completed", raw
        replay = FakeChatUpstream.seen[1][1]["messages"]
        assert replay[2] == {"role": "assistant", "content": "Running it.", "reasoning_content": "thinking",
                             "tool_calls": [{"id": "call_A", "type": "function",
                                             "function": {"name": "exec_command", "arguments": '{"cmd": "echo hi"}'},
                                             "extra_content": {"google": {"thought_signature": "SIG"}}}]}, replay[2]
        assert replay[3] == {"role": "tool", "tool_call_id": "call_A", "content": "hi\n"}, replay[3]

        status, raw = post(dict(first, model="bad"))
        assert status == 400 and json.loads(raw)["error"]["message"] == "rejected", (status, raw)
        # A Bedrock throttle arrives from Forge as a 400 "rate limit, quota, or billing
        # limit"; the relay retries it like a 429 and the client never sees it (2026-09-24).
        n_before = len(FakeChatUpstream.seen)
        status, raw = post(dict(first, model="quota-once"))
        assert status == 200 and _sse_events(raw)[-1]["type"] == "response.completed", (status, raw[:300])
        assert len(FakeChatUpstream.seen) == n_before + 2, "the quota 400 is retried upstream"
        status, raw = post(dict(first, model="cut"))
        last = _sse_events(raw)[-1]
        assert last["type"] == "response.incomplete" and \
            last["response"]["incomplete_details"] == {"reason": "max_output_tokens"}, last
        status, raw = post(dict(first, model="malformed"))
        last = _sse_events(raw)[-1]
        assert last["type"] == "response.failed" and "MALFORMED_FUNCTION_CALL" in last["response"]["error"]["message"], last

        # A Claude model on the chat wire (Forge) asks for the thinking summary so the
        # upstream connection stays busy through a long think (responses_to_chat, 2026-09-23).
        status, raw = post(dict(first, model="tensorblock/claude-fable-5-1"))
        assert status == 200 and _sse_events(raw)[-1]["type"] == "response.completed", raw
        assert FakeChatUpstream.seen[-1][1]["thinking"] == {"type": "adaptive", "display": "summarized"}
        # ... and cache breakpoints on the system message and the trailing user message,
        # the only way Forge caches (2026-09-24). Non-Claude requests above are untouched.
        claude_sent = FakeChatUpstream.seen[-1][1]["messages"]
        assert claude_sent[0] == {"role": "system", "content": [
            {"type": "text", "text": "INS\n\nDEV", "cache_control": {"type": "ephemeral"}}]}, claude_sent[0]
        assert claude_sent[-1] == {"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}, claude_sent[-1]
        # A replay ending in a tool result marks the last assistant text, never the tool message.
        status, raw = post(dict(first, model="tensorblock/claude-fable-5-1", input=[dev, user] + echoed))
        assert status == 200, raw[:200]
        replay = FakeChatUpstream.seen[-1][1]["messages"]
        assert replay[-1] == {"role": "tool", "tool_call_id": "call_A", "content": "hi\n"}, replay[-1]
        assert replay[-2]["role"] == "assistant" and replay[-2]["content"] == [
            {"type": "text", "text": "Running it.", "cache_control": {"type": "ephemeral"}}], replay[-2]

        # Codex's context compaction sends the tool history with no tools, which Bedrock
        # refuses; the attempt's last tool list goes back with it, no tool_choice (2026-09-24).
        compact = {k: v for k, v in first.items() if k not in ("tools", "tool_choice", "parallel_tool_calls")}
        status, raw = post(dict(compact, input=[dev, user] + echoed
                                + [{"type": "message", "role": "user", "content": "COMPACT"}]))
        assert status == 200 and _sse_events(raw)[-1]["type"] == "response.completed", raw
        sent = FakeChatUpstream.seen[-1][1]
        assert [t["function"]["name"] for t in sent["tools"]] == ["exec_command"], sent
        assert "tool_choice" not in sent and "parallel_tool_calls" not in sent, sent

        time.sleep(0.3)
        recs = [json.loads(l) for l in open(out)]
        assert all(r["wire"] == "chat" for r in recs) and recs[0]["request"] == first
        assert recs[0]["upstream_request"]["messages"][0]["role"] == "system"
        assert "data:" in recs[0]["upstream_response"]["_raw_text"]
        assert recs[0]["translation_notes"] == ["tool of type web_search dropped"]
        assert recs[0]["request_headers"]["authorization"] == "<scrubbed>"
        print("ok: chat wire translates requests and streamed replies, replays turns verbatim, passes errors")
    finally:
        relay.terminate()
        up.shutdown()


if __name__ == "__main__":
    main()
