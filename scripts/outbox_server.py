#!/usr/bin/env python3
"""Read-only outbox server for the Grok Bot gateway.

Serves exactly two kinds of path, nothing else:
    GET|HEAD /<uuidv4>.json         -> the result file for that request
    GET|HEAD /<uuidv4>.reply.json   -> a relayed reply for that request
Everything else (including "/", ".part" files, other methods) is 404/405.
There is no directory listing. Request ids are caller-generated UUIDv4s and act
as capability tokens, so ids are never written to the log.

A background thread deletes outbox files older than MAX_AGE_HOURS.
Stdlib only. Binds loopback, plus the tailnet IP when start.sh finds one.
(`tailscale serve` was not used: it routes by Host name, so http://<tailnet-IP>:PORT 404s.)

Tunnel mode: if GROKGW_OUTBOX_TOKEN or GROKGW_OUTBOX_TOKEN_FILE is set, every
request must carry `Authorization: Bearer <token>` (constant-time compare) or
it gets 401; there are no unauthenticated paths then. Exposes results on the
public internet, so rotate the token and keep it out of logs.
"""
import hmac
import http.server
import os
import re
import socketserver
import stat
import sys
import threading
import time

# Default matches start.sh (GROKGW_HOME=/workspace/gateway); override with GROKGW_OUTBOX_DIR.
OUTBOX = os.environ.get("GROKGW_OUTBOX_DIR", "/workspace/gateway/outbox")
# Comma-separated bind addresses, e.g. "127.0.0.1,<tailnet-ip>".
HOSTS = [h.strip() for h in os.environ.get("GROKGW_BIND", "127.0.0.1").split(",") if h.strip()]
PORT = int(os.environ.get("GROKGW_PORT", "8787"))
MAX_AGE_HOURS = float(os.environ.get("GROKGW_MAX_AGE_HOURS", "24"))
CLEAN_EVERY_S = int(os.environ.get("GROKGW_CLEAN_EVERY_S", "600"))
MAX_BYTES = 32 * 1024 * 1024


def _load_token():
    # Tunnel mode: require a bearer token on every request. Token is held in memory and never logged.
    tok = os.environ.get("GROKGW_OUTBOX_TOKEN")
    if tok:
        return tok.encode("utf-8")
    path = os.environ.get("GROKGW_OUTBOX_TOKEN_FILE")
    if path:
        try:
            with open(path, "rb") as f:
                return f.read().strip()
        except OSError:
            sys.stderr.write("outbox: token file unreadable; refusing to start without a token\n")
            sys.exit(2)
    return None


TOKEN = _load_token()

PATH_RE = re.compile(
    r"^/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})(\.reply)?\.json$"
)


def cleanup_once(now=None):
    now = now or time.time()
    cutoff = now - MAX_AGE_HOURS * 3600
    removed = 0
    try:
        names = os.listdir(OUTBOX)
    except FileNotFoundError:
        return 0
    for name in names:
        if not (name.endswith(".json") or name.endswith(".part")):
            continue
        p = os.path.join(OUTBOX, name)
        try:
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                os.unlink(p)
                removed += 1
        except FileNotFoundError:
            pass
    return removed


def cleaner():
    while True:
        try:
            n = cleanup_once()
            if n:
                log(f"cleanup removed={n}")
        except Exception as e:  # never kill the server over cleanup
            log(f"cleanup error={type(e).__name__}")
        time.sleep(CLEAN_EVERY_S)


def log(msg):
    sys.stderr.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")
    sys.stderr.flush()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "outbox"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # default logger would print the id
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)
        log(f"{self.command} status={code} kind={getattr(self, '_kind', '-')}")

    def _serve(self):
        self._kind = "-"
        if TOKEN is not None:
            got = self.headers.get("Authorization", "") or ""
            if not hmac.compare_digest(got, "Bearer " + TOKEN.decode("utf-8", "replace")):
                return self._send(401, b'{"error":"unauthorized"}')
        path = self.path.split("?", 1)[0]
        m = PATH_RE.match(path)
        if not m:
            return self._send(404, b'{"error":"not_found"}')
        self._kind = "reply" if m.group(2) else "result"
        fname = m.group(1) + (".reply" if m.group(2) else "") + ".json"
        p = os.path.join(OUTBOX, fname)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return self._send(404, b'{"error":"not_found"}')
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return self._send(404, b'{"error":"not_found"}')
        try:
            with open(p, "rb") as f:
                body = f.read(MAX_BYTES + 1)
        except OSError:
            return self._send(404, b'{"error":"not_found"}')
        return self._send(200, body)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()

    def _no(self):
        self._kind = "-"
        self._send(405, b'{"error":"method_not_allowed"}')

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no


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
    os.makedirs(OUTBOX, exist_ok=True)
    threading.Thread(target=cleaner, daemon=True).start()
    servers = [Server((h, PORT), Handler) for h in HOSTS]
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"listening {','.join(HOSTS)} port={PORT} outbox={OUTBOX} max_age_h={MAX_AGE_HOURS}")
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
