"""Check local FastAPI readiness and the authenticated HTTPS report route."""

import argparse
import base64
from datetime import date, datetime
import getpass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
import socket
import ssl
from time import monotonic
from urllib.parse import urlsplit
import warnings


READY = "/api/v1/health/ready"
TODAY = "/api/v1/reports/today"
PRIVATE_READ_PATHS = ("/openapi.json", "/docs", READY, "/api/v1/news/sources/health",
                      "/api/v1/reports/evaluation", "/internal/miniprogram/wechat-read-auth")
AUTHENTICATED_READ_PATHS = (TODAY, "/api/v1/reports", "/api/v1/news",
                            "/api/v1/news/00000000-0000-0000-0000-000000000001/timeline")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EXIT_CODES = {"ok": 0, "degraded": 1, "failed": 2}


def get_json(connection, path, *, headers=None, read_body=True):
    start = monotonic()
    result = {}
    payload = None
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        result["http_status"] = response.status
        if (read_body and response.status in (200, 404)
                and response.headers.get_content_type() == "application/json"):
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                result["reason"] = "response_too_large"
            else:
                try:
                    payload = json.loads(body)
                except (ValueError, RecursionError):
                    result["reason"] = "invalid_json"
    except ssl.SSLCertVerificationError:
        result["reason"] = "tls_verification_failed"
    except socket.gaierror:
        result["reason"] = "dns_resolution_failed"
    except ssl.SSLError:
        result["reason"] = "tls_handshake_failed"
    except TimeoutError:
        result["reason"] = "timeout"
    except (HTTPException, OSError):
        result["reason"] = "connection_failed"
    finally:
        connection.close()
    result["elapsed_ms"] = round((monotonic() - start) * 1000)
    return result, payload if isinstance(payload, dict) else {}


def check_readiness(port, timeout):
    result, payload = get_json(HTTPConnection("127.0.0.1", port, timeout=timeout), READY)
    status = result.get("http_status")
    checks = payload.get("checks")
    if (status == 200 and payload.get("status") in ("ok", "degraded")
            and isinstance(checks, dict) and checks.get("database") is True):
        result["status"] = payload["status"]
        for key in ("database", "bm25_ready", "scheduler_running"):
            value = checks.get(key)
            if type(value) is bool or value == "unavailable":
                result[key] = value
    else:
        result["status"] = "failed"
        result.setdefault("reason", "upstream_unavailable" if status == 503
                          else "unexpected_readiness_response")
    return result


def report_day(payload):
    try:
        business_day = payload.get("report_day")
        if business_day is not None:
            return date.fromisoformat(business_day).isoformat()
        return datetime.fromisoformat(payload["date"]).date().isoformat()
    except (TypeError, ValueError, KeyError):
        return None


def check_gateway(host, port, context, authorization, timeout):
    connection = HTTPSConnection(host, port, context=context, timeout=timeout)
    result, payload = get_json(connection, TODAY, headers={"Authorization": authorization})
    status = result.get("http_status")
    day = report_day(payload)
    if (status == 200 and payload.get("report_type") == "daily" and day
            and isinstance(payload.get("content"), dict)):
        result.update(status="ok", report_day=day)
    elif status == 404 and payload.get("detail") == "暂无研判报告，请先触发生成":
        result.update(status="degraded", reason="no_daily_report")
    else:
        result["status"] = "failed"
        result.setdefault("reason", "authentication_failed" if status in (401, 403)
                          else "unexpected_report_response")
    return result


def check_boundary(host, port, context, authorization, timeout):
    checks = {}
    cases = [(path, None, 401) for path in AUTHENTICATED_READ_PATHS]
    cases.extend((path, authorization, 404) for path in PRIVATE_READ_PATHS)
    for path, credential, expected in cases:
        connection = HTTPSConnection(host, port, context=context, timeout=timeout)
        headers = {"Authorization": credential} if credential else None
        result, _ = get_json(connection, path, headers=headers, read_body=False)
        passed = result.get("http_status") == expected and "reason" not in result
        result.update(status="ok" if passed else "failed", expected_status=expected)
        if not passed:
            result.setdefault("reason", "unexpected_access_status")
        checks[path] = result
        if "http_status" not in result:
            break
    return {"status": "failed" if any(check["status"] == "failed" for check in checks.values()) else "ok",
            "checks": checks}


def gateway_address(value):
    try:
        url = urlsplit(value)
        port = 443 if url.port is None else url.port
        if (url.scheme == "https" and url.hostname and port > 0 and url.username is None
                and url.path in ("", "/") and not (url.query or url.fragment)):
            return url.hostname, port
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("expected an HTTPS origin without credentials, path or query")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=gateway_address, default="https://127.0.0.1:8443")
    parser.add_argument("--upstream-port", type=int, default=8000)
    parser.add_argument("--ca-file", help="CA certificate for a gateway using a local issuer")
    parser.add_argument("--prompt-password", action="store_true", help="read the password interactively")
    parser.add_argument("--gateway-only", action="store_true", help="skip the local FastAPI readiness check")
    parser.add_argument("--verify-boundary", action="store_true", help="check anonymous and private-route denials")
    parser.add_argument("--timeout", type=int, default=5, help="socket timeout in seconds")
    args = parser.parse_args()
    if not 1 <= args.upstream_port <= 65535 or args.timeout < 1:
        parser.error("provide a valid upstream port and a positive timeout")

    user = os.environ.get("MINIPROGRAM_GATEWAY_USER")
    password = os.environ.get("MINIPROGRAM_PROBE_PASSWORD")
    if args.prompt_password:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                password = getpass.getpass("Gateway password: ")
            except (getpass.GetPassWarning, EOFError):
                parser.error("password prompt requires an interactive console")
    if not user or ":" in user or not password:
        parser.error("provide MINIPROGRAM_GATEWAY_USER and a probe password")
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    try:
        context = ssl.create_default_context(cafile=args.ca_file)
    except OSError:
        parser.error("could not load the gateway CA file")

    host, port = args.gateway
    authorization = "Basic " + credentials
    readiness = {"status": "skipped"} if args.gateway_only else check_readiness(args.upstream_port, args.timeout)
    gateway = check_gateway(host, port, context, authorization, args.timeout)
    output = {"readiness": readiness, "gateway": gateway}
    statuses = [gateway["status"]]
    if not args.gateway_only:
        statuses.append(readiness["status"])
    if args.verify_boundary:
        if gateway["status"] == "failed":
            output["boundary"] = {"status": "skipped", "reason": "gateway_check_failed"}
        else:
            output["boundary"] = check_boundary(host, port, context, authorization, args.timeout)
            statuses.append(output["boundary"]["status"])
    status = max(statuses, key=EXIT_CODES.get)
    print(json.dumps({"status": status, **output}))
    return EXIT_CODES[status]


if __name__ == "__main__":
    raise SystemExit(main())
