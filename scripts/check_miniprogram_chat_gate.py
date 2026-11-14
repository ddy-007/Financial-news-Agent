"""Exercise the Caddy chat route and real quota database against a fake Agent."""

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection, HTTPSConnection
import json
from pathlib import Path
import secrets
import sqlite3
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from check_miniprogram_gateway import GatewayChecks, LOOPBACK, loopback_port, stop_process


CHAT_PATH = "/api/v1/agent/chat"
ROOT = Path(__file__).resolve().parents[1]


class ChatGateChecks(GatewayChecks):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(cls.enterClassContext(TemporaryDirectory(prefix="ws-chat-pilot-")))
        cls.gate_port = loopback_port()
        cls.tokens = {user: secrets.token_urlsafe(32) for user in ("alice", "bob", "charlie")}
        cls.usage = cls.tmp / "usage.sqlite3"
        super().setUpClass()
        cls.gate_env = dict(cls.env, MINIPROGRAM_CHAT_QUOTA_DB=str(cls.usage),
                            MINIPROGRAM_CHAT_USER_DAILY="2", MINIPROGRAM_CHAT_TOTAL_DAILY="4",
                            MINIPROGRAM_CHAT_USERS=json.dumps({
                                user: hashlib.sha256(token.encode()).hexdigest()
                                for user, token in cls.tokens.items()
                            }))
        cls.gate_env.pop("MINIPROGRAM_WECHAT_APP_ID", None)
        cls.gate_env.pop("MINIPROGRAM_WECHAT_APP_SECRET", None)

    @classmethod
    def caddy_environment(cls):
        return {"MINIPROGRAM_CHAT_ENABLED": "1", "MINIPROGRAM_CHAT_PORT": str(cls.gate_port)}

    def start_gate(self):
        command = [sys.executable, "-B", "-m", "uvicorn", "deploy.miniprogram.chat_gate:create_app",
                   "--factory", "--host", LOOPBACK, "--port", str(self.gate_port), "--no-access-log"]
        return self.run_gate(command)

    def run_gate(self, command):
        log_path = self.tmp / "gate.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=self.gate_env, stdout=log, stderr=log)
        self.addCleanup(stop_process, process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            if self.post()[0] == 401:
                return process
            time.sleep(0.1)
        self.fail(log_path.read_text(encoding="utf-8", errors="replace"))

    def post(self, user=None, body=b'{"query":"synthetic question"}', *, auth=None, path=CHAT_PATH, headers=None):
        authorization = "Bearer " + self.tokens[user] if user else auth
        headers = {"Content-Type": "application/json", **(headers or {})}
        if authorization:
            headers["Authorization"] = authorization
        context = ssl.create_default_context(cafile=str(self.root_ca))
        conn = HTTPSConnection(LOOPBACK, self.port, context=context, timeout=6)
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def test_pilot_authentication_and_atomic_daily_quota(self):
        self.assertEqual(self.post("alice")[0], 502)
        process = self.start_gate()
        self.assertEqual(self.post()[0], 401)
        self.assertEqual(self.post(path="/api/v1/miniprogram/login")[0], 404)
        self.assertEqual(self.post(auth="Bearer " + "x" * 43)[0], 401)
        self.assertEqual(self.post(auth=self.auth)[0], 401)
        self.assertEqual(self.post("alice", b'{"query":" "}')[0], 422)
        self.assertEqual(self.post("alice", b"x" * 65537)[0], 413)
        self.assertEqual(self.upstream.posts, [])
        with closing(sqlite3.connect(self.usage)) as db, db:
            db.execute("INSERT INTO usage VALUES ('2000-01-01', 'alice', 99)")
        self.upstream.responses[CHAT_PATH] = (200, {"answer": "fake answer", "sources": []})
        self.addCleanup(self.upstream.responses.pop, CHAT_PATH)
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(self.post, ["alice"] * 5))
        self.assertEqual(sorted(result[0] for result in results), [200, 200, 429, 429, 429])
        self.assertTrue(all(int(headers["Retry-After"]) > 0 for code, headers, _ in results if code == 429))
        self.upstream.responses[CHAT_PATH] = (500, {"detail": "private model error"})
        failure = self.post("bob")
        self.assertEqual(failure[0], 502)
        self.assertNotIn(b"private model error", failure[2])
        self.upstream.responses[CHAT_PATH] = (200, {"detail": "private wrong response"})
        failure = self.post("bob")
        self.assertEqual(failure[0], 502)
        self.assertNotIn(b"private wrong response", failure[2])
        self.assertEqual(self.post("charlie")[0], 429)
        stop_process(process)
        self.start_gate()
        self.assertEqual(self.post("charlie")[0], 429)
        with closing(sqlite3.connect(self.usage)) as db, db:
            self.assertEqual(db.execute("SELECT user, count FROM usage ORDER BY user").fetchall(),
                             [("alice", 2), ("bob", 2)])
            db.execute("BEGIN EXCLUSIVE")
            self.assertEqual(self.post("charlie")[0], 503)
        self.assertEqual(len(self.upstream.posts), 4)
        for path, authorization, body in self.upstream.posts:
            self.assertEqual(path, CHAT_PATH)
            self.assertIsNone(authorization)
            self.assertEqual(json.loads(body), {"query": "synthetic question", "chat_history": []})


class MisconfiguredGateChecks(GatewayChecks):
    @classmethod
    def caddy_environment(cls):
        return {"MINIPROGRAM_CHAT_ENABLED": "1", "MINIPROGRAM_CHAT_PORT": str(cls.upstream.server_port),
                "MINIPROGRAM_WECHAT_LOGIN_ENABLED": "1", "MINIPROGRAM_WECHAT_READ_ENABLED": "1"}

    def test_wrong_proxy_port_does_not_reach_agent_route(self):
        status, _, _ = self.request("POST", CHAT_PATH)
        self.assertEqual(status, 404)
        self.assertEqual(self.upstream.posts, [("/internal/miniprogram/chat", None, b"")])

    def test_wrong_auth_port_cannot_release_read_data(self):
        path = "/internal/miniprogram/wechat-read-auth"
        self.upstream.responses[path] = (204, b"")
        self.addCleanup(self.upstream.responses.pop, path)
        status, headers, body = self.request("GET", "/api/v1/reports/today", auth="Bearer " + "x" * 43)
        self.assertEqual(status, 502)
        self.assertIsNone(headers.get("X-Stub-Upstream"))
        self.assertEqual(body, b"")


class WeChatGateChecks(ChatGateChecks):
    @classmethod
    def caddy_environment(cls):
        return {**super().caddy_environment(), "MINIPROGRAM_WECHAT_LOGIN_ENABLED": "1",
                "MINIPROGRAM_WECHAT_READ_ENABLED": "1"}

    def setUp(self):
        self.usage = self.tmp / f"{self.id().split('.')[-1]}.sqlite3"
        self.gate_env = dict(self.gate_env, MINIPROGRAM_CHAT_QUOTA_DB=str(self.usage))
        self.upstream.posts.clear()

    def start_wechat_gate(self, *, limit, source_limit=None):
        self.gate_env.update(MINIPROGRAM_WECHAT_APP_ID="wx0123456789abcdef",
                             MINIPROGRAM_WECHAT_APP_SECRET="synthetic-secret",
                             MINIPROGRAM_WECHAT_LOGIN_PER_MINUTE=str(limit))
        self.gate_env.pop("MINIPROGRAM_CHAT_USERS", None)
        self.gate_env.pop("MINIPROGRAM_WECHAT_LOGIN_SOURCE_PER_MINUTE", None)
        if source_limit is not None:
            self.gate_env["MINIPROGRAM_WECHAT_LOGIN_SOURCE_PER_MINUTE"] = str(source_limit)
        # Replace only the external HTTPS transport. The gateway, routes and DB are real.
        fake = """
import json
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
import uvicorn
from deploy.miniprogram import wechat_login

class FakeWeChatConnection:
    def __init__(self, host, **kwargs):
        assert host == 'api.weixin.qq.com'

    def request(self, method, path):
        assert method == 'GET'
        url = urlsplit(path)
        query = parse_qs(url.query)
        assert url.path == '/sns/jscode2session'
        assert query['appid'] == ['wx0123456789abcdef']
        assert query['secret'] == ['synthetic-secret']
        assert query['grant_type'] == ['authorization_code']
        self.code = query['js_code'][0]

    def getresponse(self):
        responses = {'first': {'openid': 'openid-marker', 'session_key': 'key-marker'},
                     'again': {'openid': 'openid-marker'}, 'other': {'openid': 'other-openid'},
                     'used': {'errcode': 40163}, 'down': {'errcode': -1},
                     'blocked': {'errcode': 40226}, 'limited': {'errcode': 45011}}
        body = BytesIO(json.dumps(responses[self.code]).encode())
        body.status = 200
        return body

    def close(self):
        pass

wechat_login.HTTPSConnection = FakeWeChatConnection
uvicorn.run('deploy.miniprogram.chat_gate:create_app', factory=True, host='127.0.0.1',
            port=PORT, access_log=False)
"""
        return self.run_gate([sys.executable, "-B", "-c", fake.replace("port=PORT", f"port={self.gate_port}")])

    def login(self, code, *, headers=None):
        return self.post(body=json.dumps({"code": code}).encode(), path="/api/v1/miniprogram/login", headers=headers)

    def test_login_sessions_share_identity_quota_and_expire(self):
        self.start_wechat_gate(limit=12, source_limit=12)
        self.assertEqual(self.post(body=b"x" * 2049, path="/api/v1/miniprogram/login")[0], 413)
        self.assertEqual(self.login("invalid code")[0], 422)
        self.assertEqual(self.login("used")[0], 401)
        self.assertEqual(self.login("blocked")[0], 403)
        self.assertEqual(self.login("limited")[0], 429)
        self.assertEqual(self.login("down")[0], 502)
        self.assertEqual(self.post(path="/internal/miniprogram/wechat-login")[0], 404)
        self.assertEqual(self.upstream.posts, [])
        tokens = []
        for code in ("first", "again", "other"):
            status, headers, body = self.login(code)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            payload = json.loads(body)
            self.assertEqual(set(payload), {"access_token", "expires_in"})
            self.assertEqual(payload["expires_in"], 3600)
            tokens.append(payload["access_token"])
        self.assertEqual(len(set(tokens)), 3)
        reads = (
            "/api/v1/reports/today", "/api/v1/reports?report_type=all&limit=999",
            "/api/v1/news?keyword=chip&limit=999",
            "/api/v1/news/00000000-0000-0000-0000-000000000001/timeline?as_of=2026-11-13",
        )
        for path in reads:
            self.assertEqual(self.request("GET", path)[0], 401)
            status, headers, body = self.request("GET", path, auth="Bearer " + tokens[0])
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIsNone(headers.get("X-Miniprogram-Read-Authorized"))
            self.assertIsNone(json.loads(body)["authorization"])
        with closing(sqlite3.connect(self.usage)) as db, db:
            db.execute("BEGIN EXCLUSIVE")
            status, headers, _ = self.request("GET", reads[0], auth="Bearer " + tokens[0], timeout=8)
            self.assertEqual(status, 503)
            self.assertIsNone(headers.get("X-Stub-Upstream"))
        self.assertEqual(self.request("GET", reads[0], auth=self.auth)[0], 200)
        self.assertEqual(self.request("GET", "/internal/miniprogram/wechat-read-auth",
                                      auth="Bearer " + tokens[0])[0], 404)
        with closing(sqlite3.connect(self.usage)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wechat_login_attempts").fetchone()[0], 7)
        self.upstream.responses[CHAT_PATH] = (200, {"answer": "fake answer", "sources": []})
        self.addCleanup(self.upstream.responses.pop, CHAT_PATH)
        self.assertEqual([self.post(auth="Bearer " + tokens[index])[0] for index in (0, 1, 1, 2)],
                         [200, 200, 429, 200])
        with closing(sqlite3.connect(self.usage)) as db, db:
            self.assertEqual(sorted(row[0] for row in db.execute("SELECT count FROM usage")), [1, 2])
            rows = db.execute("SELECT digest, user FROM wechat_sessions").fetchall()
            self.assertNotIn("openid-marker", str(rows))
            for token in tokens:
                self.assertNotIn(token, str(rows))
            db.execute("UPDATE wechat_sessions SET expires_at = 0")
        self.assertEqual(self.post(auth="Bearer " + tokens[2])[0], 401)
        self.assertEqual(self.request("GET", reads[0], auth="Bearer " + tokens[2])[0], 401)
        self.assertEqual(len(self.upstream.posts), 3)
        self.assertTrue(all(not authorization for _, authorization, _ in self.upstream.posts))

    def test_login_exchange_limit_blocks_before_provider_and_survives_restart(self):
        process = self.start_wechat_gate(limit=3)
        self.assertEqual(self.login("invalid code")[0], 422)
        self.assertEqual(self.login("down")[0], 502)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(self.login, ["used"] * 6))
        self.assertEqual(sorted(status for status, _, _ in results), [401, 401, 429, 429, 429, 429])
        for status, headers, _ in results:
            if status == 429:
                self.assertTrue(1 <= int(headers["Retry-After"]) <= 60)
                self.assertEqual(headers["Cache-Control"], "no-store")

        stop_process(process)
        self.start_wechat_gate(limit=2)
        self.assertEqual(self.login("first")[0], 429)
        with closing(sqlite3.connect(self.usage)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wechat_login_attempts").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 0)
            # After lowering the limit, waiting for only the oldest slot is insufficient.
            db.execute("UPDATE wechat_login_attempts SET admitted_at = ? WHERE rowid = "
                       "(SELECT MIN(rowid) FROM wechat_login_attempts)", (time.time() - 50,))
        status, headers, _ = self.login("first")
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 10)
        with closing(sqlite3.connect(self.usage)) as db, db:
            db.execute("UPDATE wechat_login_attempts SET admitted_at = 0")
            db.execute("UPDATE wechat_login_sources SET admitted_at = 0")
        self.assertEqual(self.login("first")[0], 200)
        with closing(sqlite3.connect(self.usage)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wechat_login_attempts").fetchone()[0], 1)
            db.execute("BEGIN EXCLUSIVE")
            self.assertEqual(self.login("down")[0], 503)
        self.assertEqual(self.upstream.posts, [])

    def test_login_source_limit_overwrites_client_headers_and_preserves_global_limit(self):
        with closing(sqlite3.connect(self.usage)) as db, db:
            db.execute("CREATE TABLE wechat_login_attempts (admitted_at REAL NOT NULL)")
            db.execute("INSERT INTO wechat_login_attempts VALUES (?)", (time.time(),))
        self.start_wechat_gate(limit=4, source_limit=2)
        for claimed in ("192.0.2.1", "192.0.2.2"):
            self.assertEqual(self.login("used", headers={"X-Miniprogram-Login-Source": claimed})[0], 401)
        status, headers, _ = self.login("first", headers={"X-Miniprogram-Login-Source": "192.0.2.3",
                                                        "X-Forwarded-For": "192.0.2.3"})
        self.assertEqual(status, 429)
        self.assertTrue(1 <= int(headers["Retry-After"]) <= 60)

        # Synthetic source values injected at the isolated listener exercise multiple peers.
        def direct(source):
            conn = HTTPConnection(LOOPBACK, self.gate_port, timeout=5)
            try:
                headers = {} if source is None else {"X-Miniprogram-Login-Source": source}
                conn.request("POST", "/internal/miniprogram/wechat-login", b'{"code":"used"}', headers)
                return conn.getresponse().status
            finally:
                conn.close()

        self.assertEqual(direct(None), 503)
        self.assertEqual(direct("not-an-address"), 503)
        self.assertEqual(direct("::ffff:127.0.0.1"), 429)
        self.assertEqual(direct("192.0.2.4"), 401)
        self.assertEqual(direct("192.0.2.5"), 429)
        with closing(sqlite3.connect(self.usage)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wechat_login_attempts").fetchone()[0], 4)
            self.assertEqual(sorted(row[0] for row in db.execute(
                "SELECT COUNT(*) FROM wechat_login_sources GROUP BY source"
            )), [1, 2])
            with db:
                db.execute("UPDATE wechat_login_attempts SET admitted_at = 0")
                db.execute("UPDATE wechat_login_sources SET admitted_at = 0")
        self.assertEqual(direct("2001:db8:1::1"), 401)
        self.assertEqual(direct("2001:db8:1::2"), 401)
        self.assertEqual(direct("2001:db8:1::3"), 429)
        self.assertEqual(direct("2001:db8:2::1"), 401)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", default="caddy")
    GatewayChecks.caddy = parser.parse_args().caddy
    suite = unittest.TestSuite([
        ChatGateChecks("test_pilot_authentication_and_atomic_daily_quota"),
        MisconfiguredGateChecks("test_wrong_proxy_port_does_not_reach_agent_route"),
        MisconfiguredGateChecks("test_wrong_auth_port_cannot_release_read_data"),
        WeChatGateChecks("test_login_sessions_share_identity_quota_and_expire"),
        WeChatGateChecks("test_login_exchange_limit_blocks_before_provider_and_survives_restart"),
        WeChatGateChecks("test_login_source_limit_overwrites_client_headers_and_preserves_global_limit"),
    ])
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
