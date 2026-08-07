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
        print("ALL RELAY TESTS PASSED")
    finally:
        relay.terminate()
        up.shutdown()


if __name__ == "__main__":
    main()
