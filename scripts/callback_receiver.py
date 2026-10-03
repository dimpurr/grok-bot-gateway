#!/usr/bin/env python3
"""Caller-side callback receiver for the Grok Bot gateway (stdlib only).

Sits behind the caller's own HTTPS reverse proxy / tunnel and receives
deliveries from the host's gateway_deliver.py for callback mode. It writes them
into GROKGW_CALLBACK_DIR as <rid>.json (result) or <rid>.reply.json (reply),
mode 0600, via .part + rename; first write wins (later duplicates -> 409).

Security:
    * Every POST must carry X-Grokgw-Timestamp (unix seconds, +/-600 s) and
      X-Grokgw-Signature: "sha256=" + hex(HMAC-SHA256(secret,
      timestamp + "." + raw_body)), where the secret is read from
      GROKGW_CALLBACK_DIR/<rid>.secret (written by the client for that request).
    * Bodies are capped at 4 MB and must be a JSON object whose request_id
      matches the path. Missing secret file -> 404; bad/missing signature ->
      401; bad body -> 400.
    * Nothing here logs request ids or bodies. Server header is exactly
      "grokgw-callback" so `grokgw doctor` can recognise the receiver.

Environment:
    GROKGW_CALLBACK_BIND   bind address (default 127.0.0.1)
    GROKGW_CALLBACK_PORT   port (default 8788)
    GROKGW_CALLBACK_DIR    where secret and result files live
                           (default <GROKGW_STATE_DIR>/callback)
    GROKGW_STATE_DIR       client state dir (default <XDG_STATE_HOME>/grokgw)
"""
import hashlib
import hmac
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("GROKGW_CALLBACK_BIND", "127.0.0.1")
PORT = int(os.environ.get("GROKGW_CALLBACK_PORT", "8788"))
_STATE = os.environ.get("GROKGW_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "grokgw"
)
DIR = os.environ.get("GROKGW_CALLBACK_DIR") or os.path.join(_STATE, "callback")
MAX_BYTES = 4 * 1024 * 1024
SKEW_S = 600

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
PATH_RE = re.compile(r"^/(%s)/(result|reply)$" % _UUID)


def valid_signature(secret, ts, raw, sig):
    if not ts or not sig or not sig.startswith("sha256="):
        return False
    try:
        t = int(ts)
    except ValueError:
        return False
    if abs(time.time() - t) > SKEW_S:
        return False
    mac = hmac.new(secret, ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig[7:], mac)


class Handler(BaseHTTPRequestHandler):
    server_version = "grokgw-callback"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # never log ids or bodies
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/healthz":
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not_found"})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        m = PATH_RE.match(self.path.split("?", 1)[0])
        if not m:
            return self._json(404, {"error": "not_found"})
        rid, kind = m.group(1), m.group(2)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"error": "bad_request"})
        if n < 0:
            return self._json(400, {"error": "bad_request"})
        if n > MAX_BYTES:
            return self._json(413, {"error": "too_large"})
        raw = self.rfile.read(n) if n else b""

        try:
            with open(os.path.join(DIR, rid + ".secret"), "rb") as f:
                secret = f.read().strip()
        except OSError:
            return self._json(404, {"error": "not_found"})

        if not valid_signature(secret, self.headers.get("X-Grokgw-Timestamp", ""),
                               raw, self.headers.get("X-Grokgw-Signature", "")):
            return self._json(401, {"error": "unauthorized"})

        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "bad_request"})
        if not isinstance(obj, dict) or obj.get("request_id") != rid:
            return self._json(400, {"error": "bad_request"})

        target = os.path.join(DIR, rid + (".reply.json" if kind == "reply" else ".json"))
        if os.path.exists(target):
            return self._json(409, {"error": "conflict"})
        os.makedirs(DIR, exist_ok=True)
        tmp = target + ".part"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp, target)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return self._json(500, {"error": "internal"})
        return self._json(200, {"ok": True})

    def _no(self):
        self._json(405, {"error": "method_not_allowed"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no


def main():
    os.makedirs(DIR, exist_ok=True)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    sys.stderr.write("grokgw-callback listening on %s:%d dir=%s\n" % (BIND, PORT, DIR))
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
