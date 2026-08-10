#!/usr/bin/env python3
"""Trajectory-recording relay (runs INSIDE the sandbox container).

The coding CLI is pointed at http://127.0.0.1:$TRAJ_PORT via its base-URL env;
this relay forwards every call to $TRAJ_UPSTREAM (the vendor API) and appends
one record per call to $TRAJ_PATH:

  {"step", "ts", "method", "path", "request_headers", "request",
   "status", "response", "latency_ms"}

request/response are parsed JSON when possible; SSE streams are stored as raw
text. Auth headers are scrubbed. Streaming responses are passed through
chunk-by-chunk, so agent latency is unaffected. Stdlib only.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ["TRAJ_UPSTREAM"].rstrip("/")
OUT = os.environ.get("TRAJ_PATH", "/trajectory/trajectory.jsonl")
PORT = int(os.environ.get("TRAJ_PORT", "9877"))

SCRUB = {"authorization", "x-api-key", "cookie", "set-cookie", "openai-organization"}
HOP = {"transfer-encoding", "content-length", "connection", "keep-alive", "content-encoding"}

_lock = threading.Lock()
_step = [0]


def _jsonable(raw: bytes, content_type: str):
    if not raw:
        return None
    if "json" in (content_type or ""):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    try:
        return {"_raw_text": raw.decode()}
    except UnicodeDecodeError:
        return {"_raw_b64": base64.b64encode(raw).decode()}


def record(entry: dict):
    with _lock:
        _step[0] += 1
        entry["step"] = _step[0]
        with open(OUT, "a") as f:
            f.write(json.dumps(entry) + "\n")


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; relay.log carries errors only
        pass

    def _handle(self):
        t0 = time.time()
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "accept-encoding")}
        fwd_headers["accept-encoding"] = "identity"
        req = urllib.request.Request(UPSTREAM + self.path,
                                     data=body if body else None,
                                     headers=fwd_headers, method=self.command)
        chunks = []
        try:
            try:
                resp = urllib.request.urlopen(req, timeout=3600)
            except urllib.error.HTTPError as e:
                resp = e
            status = resp.code
            rheaders = list(resp.headers.items())
            self.send_response(status)
            for k, v in rheaders:
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except Exception as e:  # noqa: BLE001 — recorded, then propagated as 502
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass
            status = -1
            rheaders = [("x-relay-error", repr(e))]

        raw = b"".join(chunks)
        ct = dict((k.lower(), v) for k, v in rheaders).get("content-type", "")
        record({
            "ts": datetime.now(timezone.utc).isoformat(),
            "method": self.command,
            "path": self.path,
            "request_headers": {k.lower(): ("<scrubbed>" if k.lower() in SCRUB else v)
                                for k, v in self.headers.items()},
            "request": _jsonable(body, self.headers.get("content-type", "")),
            "status": status,
            "response": _jsonable(raw, ct),
            "latency_ms": round((time.time() - t0) * 1000),
        })

    do_POST = do_GET = do_PUT = do_DELETE = _handle


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Relay)
    print(f"traj_relay: 127.0.0.1:{PORT} -> {UPSTREAM}, recording to {OUT}", flush=True)
    server.serve_forever()
