"""The MCP server's stderr must be drained (2026-09-19).

The server logs a line per request to stderr. The client opened that pipe and
never read it, so a session froze on the call that filled the 64 KB buffer
(the 1,505th with the real server): the tool call hung to its timeout, the
server was killed and the iteration lost. A chatty fake server reproduces it
in a fraction of the calls.
"""
import io
import contextlib
import textwrap

from excel_cli_agent.mcp_client import ExcelMCPClient

FAKE_SERVER = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        req = json.loads(line)
        sys.stderr.write("x" * 199 + "\\n"); sys.stderr.flush()   # 200 bytes of log per request
        if "id" not in req:
            continue
        method = req["method"]
        if method == "tools/list":
            result = {"tools": [{"name": "ping"}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": json.dumps({"success": True})}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\\n")
        sys.stdout.flush()
''')


def test_a_long_session_does_not_freeze_on_a_full_stderr_pipe(tmp_path):
    server = tmp_path / "fake_server.py"
    server.write_text(FAKE_SERVER)
    client = ExcelMCPClient(str(server), str(tmp_path))
    client.TOOL_TIMEOUTS = {"default": 10}          # instance-level: fail fast if it does freeze
    with contextlib.redirect_stdout(io.StringIO()):
        client.connect()
        try:
            # 1,000 calls x 200 bytes = 200 KB of stderr, three times the pipe buffer
            for i in range(1000):
                result = client.call_tool("ping", {})
                assert result.get("success"), f"call {i + 1} failed: {result.get('error')}"
        finally:
            client.disconnect()
    assert 0 < len(client._stderr_tail) <= 200       # last lines kept for diagnostics, bounded
    assert client._stderr_tail[-1] == "x" * 199
