#!/usr/bin/env python3
"""Subprocess-driven tests for the grok Bot gateway skill.

Everything talks to 127.0.0.1 only: a local mock webhook/GitHub server
(tests/mock_server.py), the real outbox_server.py, the real
callback_receiver.py and the real gateway_deliver.py. No real webhook, no real
GitHub, no real Grok Bot. Each test gets a fresh temp state dir and its own
servers.
"""
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
GROKGW = os.path.join(SCRIPTS, "grokgw")
MOCK = os.path.join(HERE, "mock_server.py")
OUTBOX = os.path.join(SCRIPTS, "outbox_server.py")
DELIVER = os.path.join(SCRIPTS, "gateway_deliver.py")
RECEIVER = os.path.join(SCRIPTS, "callback_receiver.py")

try:
    import jsonschema
except Exception:  # pragma: no cover
    jsonschema = None

ZERO = "00000000-0000-4000-8000-000000000000"
ALPHA_RID = "11111111-1111-4111-8111-111111111111"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(url, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except urllib.error.HTTPError as e:
            e.close()
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not come up: " + url)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


def sign(secret, body, ts=None):
    ts = ts or str(int(time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, "sha256=" + mac


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state")
        self.outbox = os.path.join(self.dir, "outbox")
        self.gh = os.path.join(self.dir, "gh")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        self.tmp.cleanup()

    def start(self, cmd, env=None):
        e = dict(os.environ)
        e.update(env or {})
        p = subprocess.Popen(cmd, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def start_mock(self, reply_field=None, sleep=None):
        port = free_port()
        env = {"GROKGW_MOCK_STATEDIR": self.dir, "GROKGW_MOCK_OUTBOX": self.outbox,
               "GROKGW_MOCK_GH_STORE": self.gh, "GROKGW_MOCK_PORT": str(port)}
        if reply_field:
            env["GROKGW_MOCK_REPLY_FIELD"] = reply_field
        if sleep is not None:
            env["GROKGW_MOCK_SLEEP"] = str(sleep)
        self.start([sys.executable, MOCK], env)
        wait_http("http://127.0.0.1:%d/healthz" % port)
        return "http://127.0.0.1:%d" % port

    def start_outbox(self):
        port = free_port()
        self.start([sys.executable, OUTBOX], {
            "GROKGW_BIND": "127.0.0.1", "GROKGW_PORT": str(port), "GROKGW_OUTBOX_DIR": self.outbox})
        wait_http("http://127.0.0.1:%d/%s.json" % (port, ZERO))
        return "http://127.0.0.1:%d" % port

    def start_receiver(self):
        port = free_port()
        cbdir = os.path.join(self.dir, "callback")
        os.makedirs(cbdir, exist_ok=True)
        self.start([sys.executable, RECEIVER], {
            "GROKGW_CALLBACK_BIND": "127.0.0.1", "GROKGW_CALLBACK_PORT": str(port),
            "GROKGW_CALLBACK_DIR": cbdir})
        wait_http("http://127.0.0.1:%d/healthz" % port)
        self.cbdir = cbdir
        return port

    def env(self, **over):
        e = dict(os.environ)
        e.update({"GROKGW_WEBHOOK_URL": "https://example.com/hook",
                  "GROKGW_WEBHOOK_KEY": "test-key", "GROKGW_STATE_DIR": self.state,
                  "GROKGW_POST_TIMEOUT": "5"})
        e.pop("GROKGW_RETURN", None)
        e.update(over)
        return e

    def run_grokgw(self, *args, env=None):
        e = env if env is not None else self.env()
        return subprocess.run(["bash", GROKGW, *args], env=e, capture_output=True, text=True, timeout=90)

    def set_status(self, value):
        with open(os.path.join(self.dir, "next_status"), "w", encoding="utf-8") as f:
            f.write(value)

    def log(self):
        return read_jsonl(os.path.join(self.dir, "requests.jsonl"))


class TestRequestId(Base):
    def test_rid_printed_before_post(self):
        mock = self.start_mock()
        r = self.run_grokgw("ping", "--no-wait", env=self.env(GROKGW_WEBHOOK_URL=mock + "/hook"))
        self.assertEqual(r.returncode, 0, r.stderr)
        entries = self.log()
        self.assertEqual(len(entries), 1)
        rid = entries[0]["request_id"]
        self.assertIn("request_id=" + rid, r.stderr)
        self.assertIn(rid, r.stdout)

    def test_crash_prints_rid(self):
        port = free_port()
        r = self.run_grokgw("ping", "--no-wait", env=self.env(
            GROKGW_WEBHOOK_URL="http://127.0.0.1:%d/hook" % port, GROKGW_POST_TIMEOUT="1"))
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertRegex(r.stderr, r"request_id=[0-9a-f-]{36}")


class TestOutcomeClassification(Base):
    def test_4xx_exit_2(self):
        mock = self.start_mock()
        self.set_status("403")
        r = self.run_grokgw("ping", "--no-wait", env=self.env(GROKGW_WEBHOOK_URL=mock + "/hook"))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("no run started", r.stderr)

    def test_5xx_exit_6(self):
        mock = self.start_mock()
        self.set_status("500")
        r = self.run_grokgw("ping", "--no-wait", env=self.env(GROKGW_WEBHOOK_URL=mock + "/hook"))
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertIn("may have started", r.stderr)

    def test_post_timeout_exit_6(self):
        mock = self.start_mock(sleep=3)
        self.set_status("sleep")
        r = self.run_grokgw("ping", "--no-wait", env=self.env(
            GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_POST_TIMEOUT="1"))
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertRegex(r.stderr, r"request_id=[0-9a-f-]{36}")


class TestCostGuard(Base):
    def test_hourly_cap(self):
        mock = self.start_mock()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_MAX_PER_HOUR="2")
        for _ in range(2):
            self.assertEqual(self.run_grokgw("ping", "--no-wait", env=env).returncode, 0)
        r = self.run_grokgw("ping", "--no-wait", env=env)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(len(self.log()), 2)

    def test_4xx_does_not_consume_cap(self):
        mock = self.start_mock()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_MAX_PER_HOUR="1")
        self.set_status("403")
        self.assertEqual(self.run_grokgw("ping", "--no-wait", env=env).returncode, 2)
        self.set_status("200")
        self.assertEqual(self.run_grokgw("ping", "--no-wait", env=env).returncode, 0)

    def test_kill_switch_env(self):
        mock = self.start_mock()
        r = self.run_grokgw("ping", "--no-wait", env=self.env(
            GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_DISABLED="1"))
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(self.log(), [])

    def test_kill_switch_file(self):
        mock = self.start_mock()
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "disabled"), "w", encoding="utf-8") as f:
            f.write("1")
        r = self.run_grokgw("ping", "--no-wait", env=self.env(GROKGW_WEBHOOK_URL=mock + "/hook"))
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(self.log(), [])


class TestNoneMode(Base):
    def test_disallowed_commands(self):
        mock = self.start_mock()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook")
        cases = [("list",), ("read", "a1"), ("ask", "a1", "hi"), ("fetch", ALPHA_RID),
                 ("send", "a1", "hi", "--wait-reply")]
        for args in cases:
            r = self.run_grokgw(*args, env=env)
            self.assertEqual(r.returncode, 7, (args, r.stderr))
            self.assertIn("needs a return path", r.stderr)
        self.assertEqual(self.log(), [])

    def test_ping_started(self):
        mock = self.start_mock()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook")
        r = self.run_grokgw("ping", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(obj["status"], "started")
        self.assertEqual(obj["return"], "none")
        body = self.log()[0]
        self.assertEqual(body["return"], {"mode": "none"})


class TestReplyNormalization(Base):
    def test_send_wait_reply_text(self):
        mock = self.start_mock(reply_field="text")
        ob = self.start_outbox()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_RETURN="tailnet", GROKGW_OUTBOX=ob)
        r = self.run_grokgw("send", "a1", "hi", "--wait-reply", "--timeout", "20", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        last = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")][-1]
        obj = json.loads(last)
        self.assertEqual(obj["reply"], "mock reply text")
        self.assertNotIn("text", obj)

    def test_ask_prints_text(self):
        mock = self.start_mock(reply_field="reply")
        ob = self.start_outbox()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_RETURN="tailnet", GROKGW_OUTBOX=ob)
        r = self.run_grokgw("ask", "a1", "hi", "--timeout", "20", "--reply-timeout", "20", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "mock reply text")


class TestTailnet(Base):
    def test_ping(self):
        mock = self.start_mock()
        ob = self.start_outbox()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_RETURN="tailnet", GROKGW_OUTBOX=ob)
        r = self.run_grokgw("ping", "--timeout", "20", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)
        self.assertEqual(obj["status"], "ok")
        self.assertTrue(obj["result"]["pong"])

    def test_read_default_limit_20(self):
        mock = self.start_mock()
        ob = self.start_outbox()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_RETURN="tailnet", GROKGW_OUTBOX=ob)
        r = self.run_grokgw("read", "a1", "--no-wait", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log()[0]["limit"], 20)


class TestGithub(Base):
    def test_fetch_via_mock(self):
        mock = self.start_mock()
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_RETURN="github",
                       GROKGW_GITHUB_REPO="owner/gateway-results", GROKGW_GITHUB_API=mock,
                       GROKGW_GITHUB_PATH="outbox", GROKGW_GITHUB_REF="main",
                       GROKGW_GITHUB_TOKEN="test-token")
        r = self.run_grokgw("ping", "--timeout", "20", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)
        self.assertEqual(obj["status"], "ok")
        gets = read_jsonl(os.path.join(self.dir, "gh_gets.jsonl"))
        self.assertTrue(any("application/vnd.github.raw" in g["accept"] for g in gets))


class TestCallbackReceiver(Base):
    def test_receiver_and_deliver(self):
        port = self.start_receiver()
        rid = ALPHA_RID
        secret = "a" * 64
        with open(os.path.join(self.cbdir, rid + ".secret"), "w", encoding="utf-8") as f:
            f.write(secret)
        body = json.dumps({"v": 1, "request_id": rid, "op": "ping", "status": "ok",
                           "completed_at": "2026-10-03T00:00:00Z"}).encode()
        ts, sig = sign(secret, body)
        base = "http://127.0.0.1:%d/%s/result" % (port, rid)

        code, _ = http(base, data=body, method="POST", headers={
            "X-Grokgw-Timestamp": ts, "X-Grokgw-Signature": sig, "Content-Type": "application/json"})
        self.assertEqual(code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.cbdir, rid + ".json")))
        code, _ = http(base, data=body, method="POST", headers={
            "X-Grokgw-Timestamp": ts, "X-Grokgw-Signature": sig, "Content-Type": "application/json"})
        self.assertEqual(code, 409)
        code, _ = http(base, data=body, method="POST", headers={
            "X-Grokgw-Timestamp": ts, "X-Grokgw-Signature": "sha256=dead", "Content-Type": "application/json"})
        self.assertEqual(code, 401)
        code, _ = http("http://127.0.0.1:%d/%s/result" % (port, ZERO), data=body, method="POST",
                       headers={"X-Grokgw-Timestamp": ts, "X-Grokgw-Signature": sig})
        self.assertEqual(code, 404)

    def test_deliver_result(self):
        port = self.start_receiver()
        rid = ALPHA_RID
        secret = "b" * 64
        with open(os.path.join(self.cbdir, rid + ".secret"), "w", encoding="utf-8") as f:
            f.write(secret)
        host = os.path.join(self.dir, "host.json")
        with open(host, "w", encoding="utf-8") as f:
            json.dump({"allowed_returns": ["callback"], "outbox_dir": self.outbox,
                       "callback": {"hosts": ["127.0.0.1"]}}, f)
        request = {"v": 1, "request_id": rid, "op": "ping",
                   "return": {"mode": "callback", "url": "http://127.0.0.1:%d" % port, "secret": secret}}
        result = {"v": 1, "request_id": rid, "op": "ping", "status": "ok",
                  "completed_at": "2026-10-03T00:00:00Z", "result": {"pong": True}, "error": None}
        rq = os.path.join(self.dir, "req.json")
        rs = os.path.join(self.dir, "res.json")
        for path, obj in ((rq, request), (rs, result)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f)
        env = self.env(GROKGW_HOME=self.dir, GROKGW_HOST_CONFIG=host,
                       GROKGW_DELIVER_INSECURE_TEST="1")
        r = subprocess.run([sys.executable, DELIVER, "result", rq, rs], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(json.loads(r.stdout)["delivered"], True)
        self.assertTrue(os.path.exists(os.path.join(self.cbdir, rid + ".json")))


class TestDeliverCheck(Base):
    def _host(self, **over):
        cfg = {"allowed_returns": ["none", "tailnet", "github"],
               "outbox_dir": self.outbox,
               "github": {"repos": {"owner/gateway-results": {"path": "outbox", "branch": "main"}},
                          "token_file": os.path.join(self.dir, "token")},
               "callback": {"hosts": ["hooks.example.com"]},
               "legacy_no_return": "tailnet"}
        cfg.update(over)
        path = os.path.join(self.dir, "host.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def _check(self, host, request):
        rq = os.path.join(self.dir, "req.json")
        with open(rq, "w", encoding="utf-8") as f:
            json.dump(request, f)
        env = self.env(GROKGW_HOME=self.dir, GROKGW_HOST_CONFIG=host)
        return subprocess.run([sys.executable, DELIVER, "check", rq], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_check(self):
        host = self._host()
        base = {"v": 1, "request_id": ALPHA_RID, "op": "ping"}
        self.assertEqual(json.loads(self._check(host, dict(base, **{"return": {"mode": "none"}})).stdout)["ok"], True)
        self.assertEqual(json.loads(self._check(host, dict(base, **{"return": {"mode": "github", "repo": "owner/gateway-results"}})).stdout)["ok"], True)

        bad_host = self._check(host, dict(base, **{"return": {"mode": "callback", "url": "https://evil.example.org", "secret": "a" * 64}}))
        self.assertEqual(bad_host.returncode, 3)
        self.assertEqual(json.loads(bad_host.stdout)["code"], "return_not_allowed")

        bad_repo = self._check(host, dict(base, **{"return": {"mode": "github", "repo": "evil/repo"}}))
        self.assertEqual(bad_repo.returncode, 3)

        not_allowed = self._check(host, dict(base, **{"return": {"mode": "tunnel"}}))
        self.assertEqual(not_allowed.returncode, 3)

        list_none = self._check(host, {"v": 1, "request_id": ALPHA_RID, "op": "list_agents",
                                       "return": {"mode": "none"}})
        self.assertEqual(list_none.returncode, 3)
        self.assertIn("needs a return path", json.loads(list_none.stdout)["message"])

        dup_rid = "22222222-2222-4222-8222-222222222222"
        os.makedirs(os.path.join(self.dir, "pending"), exist_ok=True)
        with open(os.path.join(self.dir, "pending", dup_rid + ".json"), "w", encoding="utf-8") as f:
            f.write("{}")
        dup = self._check(host, {"v": 1, "request_id": dup_rid, "op": "ping", "return": {"mode": "none"}})
        self.assertEqual(dup.returncode, 3)
        self.assertEqual(json.loads(dup.stdout)["code"], "duplicate_request")


class TestDeliverResult(Base):
    def _write(self, name, obj):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def _deliver(self, request, result, allowed=("tailnet",)):
        host = self._write("host.json", {"allowed_returns": list(allowed), "outbox_dir": self.outbox})
        rq = self._write("req.json", request)
        rs = self._write("res.json", result)
        env = self.env(GROKGW_HOME=self.dir, GROKGW_HOST_CONFIG=host)
        return subprocess.run([sys.executable, DELIVER, "result", rq, rs], env=env,
                              capture_output=True, text=True, timeout=30)

    def _result(self, status):
        return {"v": 1, "request_id": ALPHA_RID, "op": "bogus", "status": status,
                "completed_at": "2026-10-03T00:00:00Z", "result": None,
                "error": {"code": "unknown_op", "message": "no"} if status == "error" else None}

    def test_invalid_request_gets_error_result_only(self):
        bad = {"v": 1, "request_id": ALPHA_RID, "op": "bogus", "return": {"mode": "tailnet"}}
        r = self._deliver(bad, self._result("ok"))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.outbox, ALPHA_RID + ".json")))
        r = self._deliver(bad, self._result("error"))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.outbox, ALPHA_RID + ".json")))

    def test_disallowed_return_delivers_nothing(self):
        bad = {"v": 1, "request_id": ALPHA_RID, "op": "bogus", "return": {"mode": "tunnel"}}
        r = self._deliver(bad, self._result("error"))
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(json.loads(r.stdout)["code"], "return_not_allowed")
        self.assertFalse(os.path.exists(self.outbox) and os.listdir(self.outbox))

    def test_none_mode_remembers_request_id(self):
        req = {"v": 1, "request_id": ALPHA_RID, "op": "ping", "return": {"mode": "none"}}
        res = {"v": 1, "request_id": ALPHA_RID, "op": "ping", "status": "ok",
               "completed_at": "2026-10-03T00:00:00Z", "result": {"pong": True}, "error": None}
        r = self._deliver(req, res, allowed=("none",))
        self.assertEqual(json.loads(r.stdout), {"delivered": False, "mode": "none"})
        r = self._deliver(req, res, allowed=("none",))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(json.loads(r.stdout)["code"], "duplicate_request")


class TestCallbackClient(Base):
    def test_secret_removed_when_nothing_sent(self):
        cbdir = os.path.join(self.dir, "cb")
        env = self.env(GROKGW_RETURN="callback", GROKGW_CALLBACK_URL="https://hooks.example.com",
                       GROKGW_CALLBACK_DIR=cbdir, GROKGW_DISABLED="1")
        r = self.run_grokgw("ping", env=env)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual([f for f in os.listdir(cbdir) if f.endswith(".secret")], [])

    def test_secret_kept_and_sent_after_post(self):
        mock = self.start_mock()
        cbdir = os.path.join(self.dir, "cb")
        env = self.env(GROKGW_WEBHOOK_URL=mock + "/hook", GROKGW_RETURN="callback",
                       GROKGW_CALLBACK_URL="https://hooks.example.com", GROKGW_CALLBACK_DIR=cbdir)
        r = self.run_grokgw("ping", "--no-wait", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.log()[0]
        rid = body["request_id"]
        with open(os.path.join(cbdir, rid + ".secret"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), body["return"]["secret"])
        self.assertEqual(body["return"]["url"], "https://hooks.example.com")


@unittest.skipUnless(jsonschema is not None, "jsonschema not importable; skipping schema tests")
class TestSchemas(Base):
    def _validator(self, name):
        with open(os.path.join(ROOT, "contracts", name), "r", encoding="utf-8") as f:
            schema = json.load(f)
        return jsonschema.Draft202012Validator(schema)

    def test_request_schema(self):
        v = self._validator("request.schema.json")
        rid = ZERO
        modes = [
            {"mode": "none"}, {"mode": "tailnet"}, {"mode": "tunnel"},
            {"mode": "github", "repo": "owner/name"},
            {"mode": "callback", "url": "https://hooks.example.com", "secret": "a" * 64},
        ]
        ops = [{"op": "ping"}, {"op": "list_agents"},
               {"op": "read_transcript", "agent": "a1", "limit": 5},
               {"op": "send_message", "agent": "a1", "message": "hi"}]
        for ret in modes:
            for op in ops:
                req = {"v": 1, "request_id": rid, **op, "return": ret}
                self.assertEqual(list(v.iter_errors(req)), [], (op, ret))
        for bad in [
            {"v": 1, "request_id": rid, "op": "ping", "return": {"mode": "unknown"}},
            {"v": 1, "request_id": rid, "op": "ping", "return": {"mode": "github", "repo": "bad repo"}},
            {"v": 1, "request_id": rid, "op": "ping", "return": {"mode": "callback", "url": "https://x"}},
            {"v": 1, "request_id": rid, "op": "ping", "return": {"mode": "none", "extra": 1}},
            {"v": 1, "request_id": rid, "op": "send_message", "agent": "a1"},
        ]:
            self.assertTrue(list(v.iter_errors(bad)), bad)

    def test_result_schema(self):
        v = self._validator("result.schema.json")
        rid = ZERO
        good = [
            {"v": 1, "request_id": rid, "op": "ping", "status": "ok",
             "completed_at": "2026-10-03T00:00:00Z", "result": {"pong": True}, "error": None},
            {"v": 1, "request_id": rid, "op": "list_agents", "status": "ok",
             "completed_at": "2026-10-03T00:00:00Z",
             "result": {"agents": [{"id": "a1", "name": "A"}]}, "error": None},
            {"v": 1, "request_id": rid, "op": "read_transcript", "status": "ok",
             "completed_at": "2026-10-03T00:00:00Z",
             "result": {"agent": "a1", "lines": []}, "error": None},
            {"v": 1, "request_id": rid, "op": "send_message", "status": "ok",
             "completed_at": "2026-10-03T00:00:00Z",
             "result": {"delivered": True, "agent": "a1"}, "error": None},
            {"v": 1, "request_id": rid, "op": "ping", "status": "error",
             "completed_at": "2026-10-03T00:00:00Z", "result": None,
             "error": {"code": "tool_error", "message": "boom"}},
            {"v": 1, "request_id": rid, "from_agent": "a1",
             "received_at": "2026-10-03T00:00:00Z", "reply": "hi"},
            {"v": 1, "request_id": rid, "from_agent": "a1",
             "received_at": "2026-10-03T00:00:00Z", "text": "hi"},
        ]
        for obj in good:
            self.assertEqual(list(v.iter_errors(obj)), [], obj)
        for bad in [
            {"v": 1, "request_id": rid, "from_agent": "a1", "received_at": "2026-10-03T00:00:00Z"},
            {"v": 1, "request_id": rid, "status": "ok"},
            {"v": 1, "request_id": "nope", "status": "ok", "op": "ping",
             "completed_at": "2026-10-03T00:00:00Z"},
        ]:
            self.assertTrue(list(v.iter_errors(bad)), bad)


class TestDoctor(Base):
    def test_doctor_never_posts_and_hides_secrets(self):
        mock = self.start_mock()
        key = "SUPERSECRETKEY_value_123"
        url = mock + "/hook"
        env = self.env(GROKGW_WEBHOOK_URL=url, GROKGW_WEBHOOK_KEY=key)
        r = self.run_grokgw("doctor", env=env)
        combined = r.stdout + r.stderr
        self.assertNotIn(key, combined)
        self.assertNotIn(url, combined)
        self.assertEqual(self.log(), [])
        self.assertIn("doctor spent no Bot usage", combined)


class TestOutboxToken(Base):
    def test_token_auth(self):
        port = free_port()
        self.start([sys.executable, OUTBOX], {
            "GROKGW_BIND": "127.0.0.1", "GROKGW_PORT": str(port),
            "GROKGW_OUTBOX_DIR": self.outbox, "GROKGW_OUTBOX_TOKEN": "s3cret"})
        wait_http("http://127.0.0.1:%d/%s.json" % (port, ZERO))
        url = "http://127.0.0.1:%d/%s.json" % (port, ZERO)
        self.assertEqual(http(url)[0], 401)
        self.assertEqual(http(url, headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(http(url, headers={"Authorization": "Bearer s3cret"})[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
