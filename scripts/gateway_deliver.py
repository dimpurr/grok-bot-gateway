#!/usr/bin/env python3
"""Host-side delivery for the Grok Bot gateway (stdlib only).

This is the ONLY way the gateway Bot hands results back. A caller must not be
able to make the Bot exfiltrate transcripts to an arbitrary destination, so the
request's `return` descriptor is validated against the host's own allowlist
(host.json) before anything is written or sent. The Bot is instructed to call
this script and never to contact URLs or repos itself.

Subcommands:
    check <request.json>
        Validate the request and its return descriptor against host.json.
        Prints one JSON line {"ok":true,"mode":...} or
        {"ok":false,"code":...,"message":...}; exit 0 accepted / 3 rejected.
        Rejects a request_id already seen (pending or outbox file).
    result <request.json> <result.json>
        Re-check the return descriptor (refused => nothing is delivered),
        validate the result minimally (v, matching request_id, status ok|error,
        size <= 2 MB) and deliver it by the request's mode. If the request
        failed op-level checks but its return descriptor is allowed, only a
        status=error result is accepted, so the caller learns why.
        Stores the return descriptor in <GROKGW_HOME>/pending/<rid>.json so a
        later reply can be routed. Prints {"delivered":true|false,"mode":...};
        exit 0 delivered / 1 error.
    reply <request_id> <reply.json>
        Load the pending descriptor, validate and normalise the reply (text ->
        reply) and deliver it as <rid>.reply.json by the same mode.
    gc
        Delete pending files older than 24 h.

Environment:
    GROKGW_HOME            host state dir (default /workspace/gateway)
    GROKGW_HOST_CONFIG     config file (default <GROKGW_HOME>/host.json)
    GITHUB_API             GitHub API base (default https://api.github.com)
    GROKGW_DELIVER_INSECURE_TEST=1
                           TEST ONLY: also allow plain-http 127.0.0.1/localhost
                           callback URLs (never use in production)

Nothing here prints tokens, secrets or full callback URLs (only the host).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HOME = os.environ.get("GROKGW_HOME", "/workspace/gateway")
CONFIG_PATH = os.environ.get("GROKGW_HOST_CONFIG", os.path.join(HOME, "host.json"))
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
INSECURE_TEST = os.environ.get("GROKGW_DELIVER_INSECURE_TEST") == "1"
MAX_RESULT_BYTES = 2 * 1024 * 1024
PENDING_MAX_AGE_S = 24 * 3600

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SECRET_RE = re.compile(r"^[0-9a-f]{64}$")
OPS = ("ping", "list_agents", "read_transcript", "send_message")
MODES = ("none", "tailnet", "tunnel", "github", "callback")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class Rejected(Exception):
    def __init__(self, message, code="invalid_request"):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


def reject(message, code="invalid_request"):
    raise Rejected(message, code)


def reject_exit(err):
    emit({"ok": False, "code": err.code, "message": err.message})
    sys.exit(3)


def fail(message, code="delivery_failed"):
    emit({"delivered": False, "code": code, "message": message})
    sys.exit(1)


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        reject_exit(Rejected("file not found: %s" % os.path.basename(path)))
    except Exception:
        reject_exit(Rejected("not valid JSON: %s" % os.path.basename(path)))


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        reject_exit(Rejected("host config not found"))
    except Exception:
        reject_exit(Rejected("host config is not valid JSON"))


def write_atomic(path, data, mode=0o600):
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def pending_path(rid):
    return os.path.join(HOME, "pending", rid + ".json")


def outbox_dir(cfg):
    return cfg.get("outbox_dir") or os.path.join(HOME, "outbox")


def request_id_of(req):
    rid = req.get("request_id") if isinstance(req, dict) else None
    if not isinstance(rid, str) or not UUID_RE.match(rid):
        reject("request_id must be a lowercase UUIDv4")
    return rid


def validate_body(req, mode):
    """Op-level checks. A failure here can still be answered with an error
    result, as long as the return descriptor itself is allowed."""
    if req.get("v") != 1:
        reject("v must be 1")
    op = req.get("op")
    if op not in OPS:
        reject("unknown op", "unknown_op")
    agent = req.get("agent")
    if op in ("read_transcript", "send_message"):
        if not isinstance(agent, str) or not (1 <= len(agent) <= 200):
            reject("op %s needs an agent (1..200 chars)" % op)
    if op == "send_message":
        msg = req.get("message")
        if not isinstance(msg, str) or not msg:
            reject("send_message needs a non-empty message")
        if len(msg) > 8000:
            reject("message must be <= 8000 chars")
    if "limit" in req and req["limit"] is not None:
        lim = req["limit"]
        if not isinstance(lim, int) or isinstance(lim, bool) or not (1 <= lim <= 200):
            reject("limit must be an integer 1..200")
    if op in ("list_agents", "read_transcript") and mode == "none":
        reject("op %s needs a return path" % op, "return_not_allowed")


def validate_return(req, cfg, check_duplicate=True):
    """Where may the result go? This is the security boundary: a descriptor
    outside the host allowlist is refused and nothing is delivered at all."""
    if not isinstance(req, dict):
        reject("request must be a JSON object")
    rid = request_id_of(req)
    descriptor = req.get("return")
    if descriptor is None:
        legacy = cfg.get("legacy_no_return")
        if not legacy:
            reject("request has no return path and the host allows no legacy default", "return_not_allowed")
        descriptor = {"mode": legacy}
    if not isinstance(descriptor, dict):
        reject("return must be an object")
    mode = descriptor.get("mode")
    if mode not in MODES:
        reject("unknown return mode", "return_not_allowed")
    if mode not in (cfg.get("allowed_returns") or []):
        reject("return mode '%s' is not allowed by this host" % mode, "return_not_allowed")
    if mode == "github":
        repo = descriptor.get("repo")
        repos = (cfg.get("github") or {}).get("repos") or {}
        if not isinstance(repo, str) or repo not in repos:
            reject("github repo '%s' is not in the host allowlist" % (repo or ""), "return_not_allowed")
    elif mode == "callback":
        validate_callback(descriptor, cfg)
    if check_duplicate:
        if os.path.exists(pending_path(rid)) or os.path.exists(os.path.join(outbox_dir(cfg), rid + ".json")):
            reject("request_id was already seen", "duplicate_request")
    return mode, descriptor


def validate_request(req, cfg, check_duplicate=True):
    mode, descriptor = validate_return(req, cfg, check_duplicate)
    validate_body(req, mode)
    return mode, descriptor


def validate_callback(descriptor, cfg):
    url = descriptor.get("url")
    secret = descriptor.get("secret")
    if not isinstance(url, str) or not url:
        reject("callback return needs a url")
    if not isinstance(secret, str) or not SECRET_RE.match(secret):
        reject("callback secret must be 64 lowercase hex chars")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        reject("callback url is malformed")
    if parts.username or parts.password:
        reject("callback url must not contain userinfo")
    if INSECURE_TEST:
        if parts.scheme not in ("https", "http"):
            reject("callback url must be https")
    else:
        if parts.scheme != "https":
            reject("callback url must be https")
        if port not in (None, 443):
            reject("callback url must use port 443 (or none)")
    host = parts.hostname or ""
    if not host:
        reject("callback url has no host")
    insecure_host = INSECURE_TEST and parts.scheme == "http" and host in ("127.0.0.1", "localhost")
    if not insecure_host and (re.fullmatch(r"[0-9.]+", host) or ":" in host):
        reject("callback url must not be an IP literal")
    hosts = [h.lower() for h in ((cfg.get("callback") or {}).get("hosts") or [])]
    if host.lower() not in hosts:
        reject("callback host is not in the host allowlist", "return_not_allowed")


def callback_headers(rid, kind, body, secret):
    ts = str(int(time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Grokgw-Request-Id": rid,
        "X-Grokgw-Kind": kind,
        "X-Grokgw-Timestamp": ts,
        "X-Grokgw-Signature": "sha256=" + mac,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not allowed", headers, fp)


def deliver(mode, descriptor, rid, kind, body, cfg, op=None):
    if mode == "none":
        return True
    suffix = ".reply.json" if kind == "reply" else ".json"
    if mode in ("tailnet", "tunnel"):
        d = outbox_dir(cfg)
        os.makedirs(d, exist_ok=True)
        write_atomic(os.path.join(d, rid + suffix), body, 0o600)
        return True
    if mode == "github":
        repo = descriptor["repo"]
        rcfg = cfg["github"]["repos"][repo]
        path = rcfg.get("path", "outbox")
        branch = rcfg.get("branch", "main")
        try:
            with open(cfg["github"]["token_file"], "r", encoding="utf-8") as f:
                token = f.read().strip()
        except Exception:
            raise RuntimeError("cannot read the github token file")
        message = " ".join(p for p in ("grokgw", kind, op, rid[:8]) if p)
        payload = json.dumps({
            "message": message,
            "content": base64.b64encode(body).decode("ascii"),
            "branch": branch,
        }).encode()
        url = "%s/repos/%s/contents/%s/%s%s" % (GITHUB_API, repo, path, rid, suffix)
        r = urllib.request.Request(url, data=payload, method="PUT")
        r.add_header("Authorization", "Bearer " + token)
        r.add_header("Accept", "application/vnd.github+json")
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                raise RuntimeError("github responded HTTP %d" % resp.status)
        return True
    if mode == "callback":
        headers = callback_headers(rid, kind, body, descriptor["secret"])
        target = "%s/%s/%s" % (descriptor["url"].rstrip("/"), rid, kind)
        r = urllib.request.Request(target, data=body, method="POST")
        for k, v in headers.items():
            r.add_header(k, v)
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(r, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                raise RuntimeError("callback responded HTTP %d" % resp.status)
        return True
    raise RuntimeError("unsupported return mode '%s'" % mode)


def describe(mode, descriptor):
    if mode == "callback":
        host = urlsplit(descriptor.get("url", "")).hostname or "?"
        return "callback host %s" % host
    if mode == "github":
        return "github repo %s" % descriptor.get("repo", "?")
    return mode


def save_pending(rid, descriptor):
    d = os.path.join(HOME, "pending")
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    write_atomic(pending_path(rid), json.dumps(descriptor).encode(), 0o600)


def cmd_check(args, cfg):
    if len(args) != 1:
        reject_exit(Rejected("usage: gateway_deliver.py check <request.json>", "usage"))
    req = load_json(args[0])
    try:
        mode, _ = validate_request(req, cfg)
    except Rejected as err:
        reject_exit(err)
    emit({"ok": True, "mode": mode})
    sys.exit(0)


def cmd_result(args, cfg):
    if len(args) != 2:
        fail("usage: gateway_deliver.py result <request.json> <result.json>", "usage")
    req = load_json(args[0])
    res = load_json(args[1])
    # The return descriptor must pass the allowlist, or nothing is delivered.
    try:
        mode, descriptor = validate_return(req, cfg)
    except Rejected as err:
        reject_exit(err)
    rid = req["request_id"]
    # A request that fails op-level checks may still get an error result back.
    try:
        validate_body(req, mode)
        body_ok = True
    except Rejected:
        body_ok = False
    if not isinstance(res, dict) or res.get("v") != 1 or res.get("request_id") != rid \
            or res.get("status") not in ("ok", "error"):
        fail("result must be v=1 with a matching request_id and status ok|error", "invalid_result")
    if not body_ok and res.get("status") != "error":
        fail("the request failed validation; only an error result may be delivered", "invalid_result")
    try:
        with open(args[1], "rb") as f:
            raw = f.read(MAX_RESULT_BYTES + 1)
    except OSError:
        fail("cannot read result file")
    if len(raw) > MAX_RESULT_BYTES:
        fail("result exceeds 2 MB", "too_large")

    if mode == "none":
        save_pending(rid, descriptor)  # remembers the id so a re-sent request is refused
        emit({"delivered": False, "mode": "none"})
        sys.exit(0)
    try:
        deliver(mode, descriptor, rid, "result", raw, cfg, op=req.get("op"))
    except urllib.error.HTTPError as e:
        fail("delivery failed (HTTP %d) to %s" % (e.code, describe(mode, descriptor)), "delivery_failed")
    except Exception as e:
        fail("delivery failed to %s: %s" % (describe(mode, descriptor), type(e).__name__), "delivery_failed")
    save_pending(rid, descriptor)
    emit({"delivered": True, "mode": mode})
    sys.exit(0)


def cmd_reply(args, cfg):
    if len(args) != 2:
        fail("usage: gateway_deliver.py reply <request_id> <reply.json>", "usage")
    rid = args[0]
    if not UUID_RE.match(rid):
        fail("request_id must be a lowercase UUIDv4", "invalid_reply")
    path = pending_path(rid)
    if not os.path.exists(path):
        fail("no pending return descriptor for %s" % rid, "no_pending")
    try:
        with open(path, "r", encoding="utf-8") as f:
            descriptor = json.load(f)
    except Exception:
        fail("pending descriptor is not valid JSON", "no_pending")
    reply = load_json(args[1])
    if not isinstance(reply, dict) or reply.get("v") != 1 or reply.get("request_id") != rid:
        fail("reply must be v=1 with a matching request_id", "invalid_reply")
    if not isinstance(reply.get("from_agent"), str) or not isinstance(reply.get("received_at"), str):
        fail("reply needs from_agent and received_at", "invalid_reply")
    if "reply" not in reply and "text" not in reply:
        fail("reply needs a reply (or a legacy text) field", "invalid_reply")
    if "reply" not in reply:
        reply["reply"] = reply.pop("text")
    body = json.dumps(reply, ensure_ascii=False).encode()

    mode = descriptor.get("mode")
    if mode not in MODES or mode == "none":
        fail("pending descriptor has no deliverable mode", "no_pending")
    try:
        deliver(mode, descriptor, rid, "reply", body, cfg)
    except urllib.error.HTTPError as e:
        fail("delivery failed (HTTP %d) to %s" % (e.code, describe(mode, descriptor)), "delivery_failed")
    except Exception as e:
        fail("delivery failed to %s: %s" % (describe(mode, descriptor), type(e).__name__), "delivery_failed")
    emit({"delivered": True, "mode": mode})
    sys.exit(0)


def cmd_gc(cfg):
    d = os.path.join(HOME, "pending")
    removed = 0
    if os.path.isdir(d):
        cutoff = time.time() - PENDING_MAX_AGE_S
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                    removed += 1
            except OSError:
                pass
    emit({"ok": True, "removed": removed})
    sys.exit(0)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.stderr.write("usage: gateway_deliver.py check|result|reply|gc ...\n")
        sys.exit(0 if args else 1)
    cmd = args[0]
    cfg = load_config()
    if cmd == "check":
        cmd_check(args[1:], cfg)
    elif cmd == "result":
        cmd_result(args[1:], cfg)
    elif cmd == "reply":
        cmd_reply(args[1:], cfg)
    elif cmd == "gc":
        cmd_gc(cfg)
    else:
        sys.stderr.write("unknown command: %s\n" % cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
