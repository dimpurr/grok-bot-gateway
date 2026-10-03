#!/usr/bin/env python3
"""Local mock for the grok Bot gateway tests (stdlib only, 127.0.0.1).

One HTTP server plays two roles on one port:

* the webhook: POST /hook records every body as a JSON line to
  $GROKGW_MOCK_LOG (default <statedir>/requests.jsonl) and replies with the
  status in <statedir>/next_status ("200", "403", "500" or "sleep"; absent =
  200). "sleep" sleeps GROKGW_MOCK_SLEEP seconds (default 5) first, to exercise
  the client POST timeout. On a 200 it simulates the gateway Bot by writing a
  result (and, for send_message, an optional reply) into the outbox dir for
  outbox modes, or into the mock GitHub store for github mode.
* a mock GitHub Contents API: GET /repos/<o>/<r> -> {"private": true};
  GET /repos/<o>/<r>/contents/<path>/<file>?ref=... -> raw file or 404;
  PUT .../contents/<path>/<file> -> store the decoded base64 body (test helper
  for gateway_deliver.py). GET contents requests are recorded to
  <statedir>/gh_gets.jsonl with the Accept header (so tests can assert the raw
  media type).

New tests start a fresh server with their own dirs; nothing here contacts the
network or a real service.
"""
import base64
import http.server
import json
import os
import socketserver
import sys
import time

STATEDIR = os.environ.get("GROKGW_MOCK_STATEDIR", "/tmp/grokgw-mock")
OUTBOX = os.environ.get("GROKGW_MOCK_OUTBOX", os.path.join(STATEDIR, "outbox"))
GH_STORE = os.environ.get("GROKGW_MOCK_GH_STORE", os.path.join(STATEDIR, "gh"))
LOG = os.environ.get("GROKGW_MOCK_LOG", os.path.join(STATEDIR, "requests.jsonl"))
GH_GETS = os.path.join(STATEDIR, "gh_gets.jsonl")
REPLY_FIELD = os.environ.get("GROKGW_MOCK_REPLY_FIELD", "")
SLEEP_S = float(os.environ.get("GROKGW_MOCK_SLEEP", "5"))
PORT = int(os.environ.get("GROKGW_MOCK_PORT", "0"))
BIND = os.environ.get("GROKGW_MOCK_BIND", "127.0.0.1")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _next_status():
    try:
        with open(os.path.join(STATEDIR, "next_status"), "r", encoding="utf-8") as f:
            return f.read().strip() or "200"
    except OSError:
        return "200"


def _append(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def _payload(op, req):
    if op == "ping":
        return {"pong": True, "gateway": "mock"}
    if op == "list_agents":
        return {"agents": [{"id": "a1", "name": "Bot A", "title": "t"}]}
    if op == "read_transcript":
        return {"agent": req.get("agent", "x"), "lines": [{"seq": 1, "message": "hi"}],
                "positions": "1-1 of 1", "next_before": None, "truncated": False}
    if op == "send_message":
        return {"delivered": True, "agent": req.get("agent", "x")}
    return {}


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "grokgw-mock"
    sys_version = ""

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b'{"ok":true}')
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) == 3 and parts[0] == "repos":
            return self._send(200, b'{"private": true, "full_name": "o/r"}')
        if len(parts) >= 5 and parts[0] == "repos" and parts[3] == "contents":
            _append(GH_GETS, {"path": path, "accept": self.headers.get("Accept", "")})
            fp = os.path.join(GH_STORE, parts[-1])
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "application/octet-stream")
            return self._send(404, b'{"message":"Not Found"}')
        return self._send(404, b'{"message":"Not Found"}')

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.strip("/").split("/") if p]
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""
        if len(parts) >= 5 and parts[0] == "repos" and parts[3] == "contents":
            try:
                obj = json.loads(raw.decode("utf-8"))
                data = base64.b64decode(obj.get("content", ""))
            except Exception:
                return self._send(400, b'{"message":"Bad Request"}')
            os.makedirs(GH_STORE, exist_ok=True)
            with open(os.path.join(GH_STORE, parts[-1]), "wb") as f:
                f.write(data)
            return self._send(201, b'{"content":{"name":"ok"}}')
        return self._send(404, b'{"message":"Not Found"}')

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""
        if path != "/hook":
            return self._send(404, b'{"error":"not_found"}')
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            req = {}
        _append(LOG, req)

        status = _next_status()
        if status == "sleep":
            time.sleep(SLEEP_S)
            status = "200"
        if status != "200":
            return self._send(int(status), b"{}")

        rid = req.get("request_id", "")
        op = req.get("op", "")
        descriptor = req.get("return") or {}
        mode = descriptor.get("mode")
        result = {"v": 1, "request_id": rid, "op": op, "status": "ok",
                  "completed_at": _now(), "result": _payload(op, req), "error": None}
        if mode == "github":
            _write(os.path.join(GH_STORE, rid + ".json"), result)
        else:
            _write(os.path.join(OUTBOX, rid + ".json"), result)

        if REPLY_FIELD and op == "send_message":
            reply = {"v": 1, "request_id": rid, "from_agent": "mock-bot",
                     "received_at": _now(), REPLY_FIELD: "mock reply text"}
            if mode == "github":
                _write(os.path.join(GH_STORE, rid + ".reply.json"), reply)
            else:
                _write(os.path.join(OUTBOX, rid + ".reply.json"), reply)
        return self._send(200, b'{"status":"started"}')


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse-DNS lookup
        # that can stall for tens of seconds on macOS (seen on GitHub runners).
        # We never use server_name, so bind without the lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main():
    os.makedirs(STATEDIR, exist_ok=True)
    os.makedirs(OUTBOX, exist_ok=True)
    os.makedirs(GH_STORE, exist_ok=True)
    srv = Server((BIND, PORT), Handler)
    sys.stderr.write("mock listening on %s:%d\n" % (BIND, srv.server_address[1]))
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
