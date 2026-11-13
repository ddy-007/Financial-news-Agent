"""Exercise the real Caddyfile against a temporary loopback upstream."""

import argparse
import base64
import json
import os
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit


CONFIG = Path(__file__).resolve().parents[1] / "deploy" / "miniprogram" / "Caddyfile"
PROBE = Path(__file__).with_name("probe_miniprogram.py")
LOOPBACK = "127.0.0.1"
TODAY = "/api/v1/reports/today"
REPORTS = "/api/v1/reports"
NEWS = "/api/v1/news"
TIMELINE = NEWS + "/8c7cf8b9-cc50-4489-b3ab-b36ed1969f22/timeline"
ERROR_KEYWORD = "private-keyword-" + secrets.token_hex(4)


class StubUpstream(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if query.get("keyword") == [ERROR_KEYWORD]:
            self.close_connection = True
            return
        default = {
            "path": url.path,
            "query": query,
            "authorization": self.headers.get("Authorization"),
        }
        status, payload = self.server.responses.get(url.path, (200, default))
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Stub-Upstream", "1")
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    def log_message(self, *_args):
        pass


def loopback_port():
    with socket.socket() as listener:
        listener.bind((LOOPBACK, 0))
        return listener.getsockname()[1]


def stop_process(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class GatewayChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(cls.enterClassContext(TemporaryDirectory(prefix="ws-miniprogram-gateway-")))
        upstream = cls.enterClassContext(ThreadingHTTPServer((LOOPBACK, 0), StubUpstream))
        upstream.responses = {}
        cls.upstream = upstream
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(thread.join, 5)
        cls.addClassCleanup(upstream.shutdown)
        cls.port = loopback_port()

        cls.password = password = secrets.token_urlsafe(18)
        cls.auth = "Basic " + base64.b64encode(f"tester:{password}".encode()).decode()
        command = [cls.caddy, "hash-password", "--algorithm", "bcrypt", "--bcrypt-cost", "4",
                   "--plaintext", password]
        hashed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
        cls.env = dict(os.environ, XDG_DATA_HOME=str(work), XDG_CONFIG_HOME=str(work),
                       MINIPROGRAM_GATEWAY_HOST=f"{LOOPBACK}:{cls.port}",
                       MINIPROGRAM_GATEWAY_BIND=LOOPBACK, MINIPROGRAM_GATEWAY_USER="tester",
                       MINIPROGRAM_GATEWAY_HASH=hashed.stdout.strip(),
                       MINIPROGRAM_UPSTREAM_PORT=str(upstream.server_port))
        args = ["--config", str(CONFIG), "--adapter", "caddyfile"]
        validated = subprocess.run([cls.caddy, "validate", *args], env=cls.env, capture_output=True,
                                   text=True, timeout=15)
        if validated.returncode:
            raise RuntimeError(validated.stderr)

        cls.log_path = work / "caddy.log"
        cls.autosave = work / "caddy" / "autosave.json"
        log = cls.enterClassContext(cls.log_path.open("wb"))
        process = subprocess.Popen([cls.caddy, "run", *args], env=cls.env, stdout=log, stderr=log)
        cls.addClassCleanup(stop_process, process)
        cls.root_ca = work / "caddy" / "pki" / "authorities" / "local" / "root.crt"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            if cls.root_ca.is_file():
                try:
                    context = ssl.create_default_context(cafile=str(cls.root_ca))
                    conn = HTTPSConnection(LOOPBACK, cls.port, context=context, timeout=1)
                    try:
                        conn.connect()
                    finally:
                        conn.close()
                    return
                except OSError:
                    pass
            time.sleep(0.1)
        raise RuntimeError(cls.log_path.read_text(encoding="utf-8", errors="replace"))

    def request(self, method, path, *, auth=None):
        context = ssl.create_default_context(cafile=str(self.root_ca))
        conn = HTTPSConnection(LOOPBACK, self.port, context=context, timeout=3)
        try:
            conn.request(method, path, headers={"Authorization": auth} if auth else {})
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def test_read_routes_require_authentication(self):
        for path in (TODAY, REPORTS, NEWS, TIMELINE):
            for auth in (None, "Basic dGVzdGVyOndyb25n"):
                with self.subTest(path=path, auth=bool(auth)):
                    status, headers, _ = self.request("GET", path, auth=auth)
                    self.assertEqual(status, 401)
                    self.assertIn("Basic", headers["WWW-Authenticate"])
                    self.assertIsNone(headers.get("X-Stub-Upstream"))

    def test_read_routes_forward_only_safe_request_shape(self):
        news_query = urlencode({"keyword": "AI 芯片+CPO", "limit": "1000", "offset": "2",
                                "start": "2026-10-01", "end": "2026-11-01", "days": "30"})
        cases = (
            (TODAY + "?extra=1", TODAY, {}),
            (REPORTS + "?report_type=all&limit=10&limit=200", REPORTS,
             {"report_type": ["daily"], "limit": ["10"]}),
            (NEWS + "?" + news_query + "&limit=1000", NEWS,
             {"keyword": ["AI 芯片+CPO"], "limit": ["20"]}),
            (TIMELINE + "?as_of=2026-11-06", TIMELINE, {"as_of": ["2026-11-06"]}),
        )
        for uri, path, query in cases:
            with self.subTest(uri=uri):
                status, headers, body = self.request("GET", uri, auth=self.auth)
                self.assertEqual(status, 200)
                self.assertEqual(headers["X-Stub-Upstream"], "1")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(json.loads(body), {"path": path, "query": query, "authorization": None})

    def test_other_routes_and_methods_never_reach_upstream(self):
        cases = (
            ("POST", "/api/v1/agent/chat"), ("POST", "/api/v1/news/collect"),
            ("POST", "/api/v1/market/collect"), ("POST", "/api/v1/reports/generate"),
            ("POST", "/api/v1/reports/weekly/generate"), ("GET", "/api/v1/reports/evaluation"),
            ("GET", "/api/v1/reports/weekly"), ("GET", "/api/v1/reports/backtest"),
            ("GET", "/api/v1/news/dates"), ("GET", "/api/v1/news/sources/health"),
            ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/api/v1/health/ready"),
            ("GET", NEWS + "/invalid/timeline"), ("GET", TIMELINE + "/extra"),
            ("GET", TODAY + "/extra"), ("POST", TODAY), ("PUT", TIMELINE),
            ("HEAD", REPORTS), ("OPTIONS", NEWS),
        )
        for method, path in cases:
            for auth in (None, self.auth):
                with self.subTest(method=method, path=path, auth=bool(auth)):
                    status, headers, _ = self.request(method, path, auth=auth)
                    self.assertEqual(status, 404)
                    self.assertIsNone(headers.get("X-Stub-Upstream"))

    def test_missing_configuration_cannot_start(self):
        user = "MINIPROGRAM_GATEWAY_USER"
        hashed = "MINIPROGRAM_GATEWAY_HASH"
        for missing in ((user,), (hashed,), (user, hashed)):
            with self.subTest(missing=missing):
                env = {key: value for key, value in self.env.items() if key not in missing}
                result = subprocess.run([self.caddy, "validate", "--config", str(CONFIG),
                                         "--adapter", "caddyfile"], env=env, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)

    def test_upstream_error_omits_request_data(self):
        status, headers, _ = self.request("GET", NEWS + "?keyword=" + ERROR_KEYWORD, auth=self.auth)
        self.assertEqual(status, 502)
        self.assertIsNone(headers.get("X-Stub-Upstream"))
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("http.log.error", log)
        self.assertNotIn(ERROR_KEYWORD, log)
        self.assertNotIn(self.auth, log)
        self.assertFalse(self.autosave.exists())

    def test_probe_classifies_readiness_and_report_independently(self):
        readiness = "/api/v1/health/ready"
        ready = {"status": "ok", "checks": {"database": True, "bm25_ready": True,
                                           "scheduler_running": True}}
        paused = {"status": "degraded", "checks": {**ready["checks"], "scheduler_running": False}}
        report = {"report_type": "daily", "report_day": "2026-11-06", "date": "2026-11-07T02:00:00",
                  "title": "report-title-marker", "content": {"raw": "report-body-marker"}}
        missing_report = {"detail": "暂无研判报告，请先触发生成"}
        cases = (
            ("ok", (200, ready), (200, report), self.password, True, 0, "ok", "ok", None),
            ("paused", (200, paused), (200, report), self.password, True, 1, "degraded", "ok", None),
            ("no report", (200, ready), (404, missing_report), self.password, True,
             1, "ok", "degraded", "no_daily_report"),
            ("wrong route", (200, ready), (404, {"detail": "Not Found"}), self.password, True,
             2, "ok", "failed", "unexpected_report_response"),
            ("wrong report type", (200, ready), (200, {**report, "report_type": "weekly"}),
             self.password, True, 2, "ok", "failed", "unexpected_report_response"),
            ("database down", (503, {}), (200, report), self.password, True, 2, "failed", "ok", None),
            ("invalid readiness", (200, {"status": "ok", "checks": {}}), (200, report),
             self.password, True, 2, "failed", "ok", None),
            ("gateway failure", (200, ready), (502, {}), self.password, True,
             2, "ok", "failed", "unexpected_report_response"),
            ("bad credentials", (200, ready), (200, report), "bad-password", True,
             2, "ok", "failed", "authentication_failed"),
            ("untrusted TLS", (200, ready), (200, report), self.password, False,
             2, "ok", "failed", "tls_verification_failed"),
            ("invalid JSON", (200, ready), (200, b"broken"), self.password, True,
             2, "ok", "failed", "invalid_json"),
            ("JSON integer exceeds parser limit", (200, ready), (200, b'{"number":' + b"9" * 5000 + b"}"),
             self.password, True, 2, "ok", "failed", "invalid_json"),
        )
        self.addCleanup(self.upstream.responses.clear)
        command = [sys.executable, "-B", str(PROBE), "--gateway", f"https://{LOOPBACK}:{self.port}",
                   "--upstream-port", str(self.upstream.server_port)]
        for name, local, via_gateway, password, trust, code, local_status, gateway_status, reason in cases:
            with self.subTest(name=name):
                self.upstream.responses.update({readiness: local, TODAY: via_gateway})
                args = command + (["--ca-file", str(self.root_ca)] if trust else [])
                env = dict(self.env, MINIPROGRAM_PROBE_PASSWORD=password, PYTHONINTMAXSTRDIGITS="4300")
                run = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, code, run.stderr)
                result = json.loads(run.stdout)
                self.assertEqual(result["status"], ("ok", "degraded", "failed")[code])
                self.assertEqual(result["readiness"]["status"], local_status)
                self.assertEqual(result["gateway"]["status"], gateway_status)
                self.assertEqual(result["gateway"].get("reason"), reason)
                if gateway_status == "ok":
                    self.assertEqual(result["gateway"]["report_day"], "2026-11-06")
                for private_value in (password, "report-title-marker", "report-body-marker"):
                    self.assertNotIn(private_value, run.stdout + run.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", default="caddy", help="Caddy executable path")
    GatewayChecks.caddy = parser.parse_args().caddy
    unittest.main(argv=[__file__], verbosity=2)
