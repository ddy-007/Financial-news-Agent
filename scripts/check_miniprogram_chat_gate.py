"""Exercise the Caddy chat route and real quota database against a fake Agent."""

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPSConnection
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

    @classmethod
    def caddy_environment(cls):
        return {"MINIPROGRAM_CHAT_ENABLED": "1", "MINIPROGRAM_CHAT_PORT": str(cls.gate_port)}

    def start_gate(self):
        command = [sys.executable, "-B", "-m", "uvicorn", "deploy.miniprogram.chat_gate:create_app",
                   "--factory", "--host", LOOPBACK, "--port", str(self.gate_port), "--no-access-log"]
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

    def post(self, user=None, body=b'{"query":"synthetic question"}', *, auth=None):
        authorization = "Bearer " + self.tokens[user] if user else auth
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        context = ssl.create_default_context(cafile=str(self.root_ca))
        conn = HTTPSConnection(LOOPBACK, self.port, context=context, timeout=6)
        try:
            conn.request("POST", CHAT_PATH, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def test_pilot_authentication_and_atomic_daily_quota(self):
        self.assertEqual(self.post("alice")[0], 502)
        process = self.start_gate()
        self.assertEqual(self.post()[0], 401)
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
        return {"MINIPROGRAM_CHAT_ENABLED": "1", "MINIPROGRAM_CHAT_PORT": str(cls.upstream.server_port)}

    def test_wrong_proxy_port_does_not_reach_agent_route(self):
        status, _, _ = self.request("POST", CHAT_PATH)
        self.assertEqual(status, 404)
        self.assertEqual(self.upstream.posts, [("/internal/miniprogram/chat", None, b"")])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", default="caddy")
    GatewayChecks.caddy = parser.parse_args().caddy
    suite = unittest.TestSuite([
        ChatGateChecks("test_pilot_authentication_and_atomic_daily_quota"),
        MisconfiguredGateChecks("test_wrong_proxy_port_does_not_reach_agent_route"),
    ])
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
