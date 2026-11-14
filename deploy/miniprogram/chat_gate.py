"""Opt-in admission control for the single-host mini program chat pilot."""

import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
from asyncio import CancelledError
from contextlib import asynccontextmanager, closing
from datetime import datetime, time, timedelta, timezone
from ipaddress import IPv6Address, ip_address, ip_network
from pathlib import Path
from time import perf_counter, time as unix_time
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .wechat_login import WeChatLoginBlocked, WeChatRateLimited, WeChatUnavailable, exchange_code


CHAT_PATH = "/api/v1/agent/chat"
ADMISSION_PATH = "/internal/miniprogram/chat"
LOGIN_PATH = "/internal/miniprogram/wechat-login"
READ_AUTH_PATH = "/internal/miniprogram/wechat-read-auth"
MAX_BODY_BYTES = 64 * 1024
SESSION_TTL = 3600
LOGIN_WINDOW_SECONDS = 60
DEFAULT_LOGIN_LIMIT = 12
DEFAULT_LOGIN_SOURCE_LIMIT = 4
BUSINESS_TZ = timezone(timedelta(hours=8))
NO_STORE = {"Cache-Control": "no-store"}
TOKEN_PATTERN = re.compile(r"Bearer ([A-Za-z0-9_-]{43})", re.IGNORECASE | re.ASCII)
REQUEST_ID_HEADER = "X-Miniprogram-Request-ID"
gate_log = logging.getLogger("uvicorn.error")


class GateError(HTTPException):
    def __init__(self, status_code, outcome, detail, headers=None):
        super().__init__(status_code, detail, {**NO_STORE, **(headers or {})})
        self.outcome = outcome


class ObservedRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def observed(request):
            request_id = uuid4().hex
            started = perf_counter()
            status, outcome = 500, "internal_error"
            try:
                response = await handle(request)
            except GateError as error:
                error.headers[REQUEST_ID_HEADER] = request_id
                status, outcome = error.status_code, error.outcome
                raise
            except CancelledError:
                status, outcome = 499, "request_cancelled"
                raise
            except ClientDisconnect:
                status, outcome = 499, "client_disconnected"
                return Response(status_code=status, headers={**NO_STORE, REQUEST_ID_HEADER: request_id})
            except Exception:
                # Exception text from network and storage clients can contain request data.
                return Response(status_code=status, headers={**NO_STORE, REQUEST_ID_HEADER: request_id})
            else:
                response.headers[REQUEST_ID_HEADER] = request_id
                status, outcome = response.status_code, getattr(request.state, "outcome", "ok")
                return response
            finally:
                gate_log.info("[miniprogram_gate] request_id=%s operation=%s status=%d outcome=%s elapsed_ms=%.1f",
                              request_id, self.name, status, outcome, (perf_counter() - started) * 1000)

        return observed


class LoginCode(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


async def read_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise GateError(413, "body_too_large", "Request body too large")
        body.extend(chunk)
    return body


def new_session(db_path, app_id, openid):
    token = secrets.token_urlsafe(32)
    user = f"wechat:{app_id}:" + hashlib.sha256(f"{app_id}:{openid}".encode()).hexdigest()
    digest = hashlib.sha256(token.encode()).hexdigest()
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
        now = int(unix_time())
        db.execute("DELETE FROM wechat_sessions WHERE expires_at <= ?", (now,))
        db.execute("INSERT INTO wechat_sessions VALUES (?, ?, ?)", (digest, user, now + SESSION_TTL))
    return token


def session_user(db_path, digest, app_id):
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT user FROM wechat_sessions WHERE digest = ? AND expires_at > ?",
                         (digest, int(unix_time()))).fetchone()
        return row[0] if row and row[0].startswith(f"wechat:{app_id}:") else None


def login_source(request):
    values = request.headers.getlist("x-miniprogram-login-source")
    try:
        if len(values) != 1 or "%" in values[0]:
            raise ValueError
        address = ip_address(values[0])
    except ValueError:
        raise GateError(503, "login_source_unavailable", "Login source unavailable") from None
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif not address.is_loopback:
            address = ip_network((address, 64), strict=False).network_address
    return hashlib.sha256(address.packed).hexdigest()


def reserve_login_exchange(db_path, limit, source, source_limit):
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = unix_time()
        db.execute("DELETE FROM wechat_login_attempts WHERE admitted_at <= ?", (now - LOGIN_WINDOW_SECONDS,))
        db.execute("DELETE FROM wechat_login_sources WHERE admitted_at <= ?", (now - LOGIN_WINDOW_SECONDS,))
        attempts = db.execute("SELECT admitted_at FROM wechat_login_attempts ORDER BY admitted_at").fetchall()
        source_attempts = db.execute("SELECT admitted_at FROM wechat_login_sources WHERE source = ? "
                                     "ORDER BY admitted_at", (source,)).fetchall()
        saturated = []
        if len(attempts) >= limit:
            saturated.append(attempts[-limit][0])
        if len(source_attempts) >= source_limit:
            saturated.append(source_attempts[-source_limit][0])
        if saturated:
            expires_from = max(saturated)
            return max(1, math.ceil(expires_from + LOGIN_WINDOW_SECONDS - now))
        db.execute("INSERT INTO wechat_login_attempts VALUES (?)", (now,))
        db.execute("INSERT INTO wechat_login_sources VALUES (?, ?)", (now, source))
    return None


class PilotQuestion(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    chat_history: list[tuple[str, str]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_content(self):
        if not self.query.strip() or sum(len(text) for turn in self.chat_history for text in turn) > 8000:
            raise ValueError("Question or history exceeds pilot limits")
        return self


def charge_request(db_path, user, user_limit, total_limit):
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = datetime.now(BUSINESS_TZ)
        day = now.date().isoformat()
        db.execute("DELETE FROM usage WHERE day < ?", (day,))
        total, personal = db.execute(
            "SELECT COALESCE(SUM(count), 0), COALESCE(SUM(CASE WHEN user = ? THEN count END), 0) "
            "FROM usage WHERE day = ?", (user, day),
        ).fetchone()
        if personal >= user_limit or total >= total_limit:
            next_day = datetime.combine(now.date() + timedelta(days=1), time.min, BUSINESS_TZ)
            return int((next_day - now).total_seconds()) + 1
        db.execute(
            "INSERT INTO usage(day, user, count) VALUES (?, ?, 1) "
            "ON CONFLICT(day, user) DO UPDATE SET count = count + 1", (day, user),
        )
    return None


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        try:
            users = json.loads(os.environ.get("MINIPROGRAM_CHAT_USERS", "{}"))
            app_id = os.environ.get("MINIPROGRAM_WECHAT_APP_ID", "")
            secret = os.environ.get("MINIPROGRAM_WECHAT_APP_SECRET", "")
            if (bool(app_id) != bool(secret) or (app_id and not re.fullmatch(r"wx[0-9a-fA-F]{16}", app_id))
                    or not isinstance(users, dict) or not (users or app_id)
                    or len(set(users.values())) != len(users)
                    or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user)
                           or not re.fullmatch(r"[a-f0-9]{64}", digest) for user, digest in users.items())):
                raise ValueError
            users_by_digest = {digest: user for user, digest in users.items()}
            db_path = Path(os.environ["MINIPROGRAM_CHAT_QUOTA_DB"])
            user_limit = int(os.environ.get("MINIPROGRAM_CHAT_USER_DAILY", "5"))
            total_limit = int(os.environ.get("MINIPROGRAM_CHAT_TOTAL_DAILY", "20"))
            login_limit = DEFAULT_LOGIN_LIMIT
            source_limit = DEFAULT_LOGIN_SOURCE_LIMIT
            if app_id:
                login_limit = int(os.environ.get("MINIPROGRAM_WECHAT_LOGIN_PER_MINUTE", DEFAULT_LOGIN_LIMIT))
                source_limit = int(os.environ.get("MINIPROGRAM_WECHAT_LOGIN_SOURCE_PER_MINUTE",
                                                  min(DEFAULT_LOGIN_SOURCE_LIMIT, login_limit)))
            port = int(os.environ.get("MINIPROGRAM_UPSTREAM_PORT", "8000"))
            if (not db_path.is_absolute() or not 1 <= user_limit <= total_limit
                    or not 1 <= source_limit <= login_limit <= 120 or not 1 <= port <= 65535):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise RuntimeError("Invalid mini program chat pilot configuration") from None

        try:
            with closing(sqlite3.connect(db_path, timeout=2)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, user TEXT, count INTEGER NOT NULL, "
                           "PRIMARY KEY (day, user))")
                db.execute("CREATE TABLE IF NOT EXISTS wechat_sessions (digest TEXT PRIMARY KEY, "
                           "user TEXT NOT NULL, expires_at INTEGER NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS wechat_login_attempts (admitted_at REAL NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS wechat_login_sources (admitted_at REAL NOT NULL, source TEXT NOT NULL)")
        except sqlite3.Error:
            raise RuntimeError("Cannot initialize mini program chat quota database") from None

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                     timeout=httpx.Timeout(175, connect=3)) as client:
            app.state.admit = (users_by_digest, db_path, user_limit, total_limit)
            app.state.wechat = (app_id, secret, login_limit, source_limit) if app_id else None
            app.state.upstream = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.router.route_class = ObservedRoute

    async def authenticated_user(request, *, allow_pilot=False):
        users, db_path, _, _ = app.state.admit
        authorization = request.headers.getlist("authorization")
        match = TOKEN_PATTERN.fullmatch(authorization[0]) if len(authorization) == 1 else None
        if match:
            digest = hashlib.sha256(match[1].encode("ascii")).hexdigest()
            if allow_pilot and digest in users:
                return users[digest]
            if app.state.wechat:
                try:
                    user = await run_in_threadpool(session_user, db_path, digest, app.state.wechat[0])
                except sqlite3.Error:
                    raise GateError(503, "session_check_unavailable", "Session check unavailable") from None
                if user:
                    return user
        raise GateError(401, "session_required", "Valid access token required", {"WWW-Authenticate": "Bearer"})

    @app.get(READ_AUTH_PATH)
    async def authorize_read(request: Request):
        await authenticated_user(request)
        return Response(status_code=204, headers={**NO_STORE, "X-Miniprogram-Read-Authorized": "1"})

    @app.post(LOGIN_PATH)
    async def login(request: Request, response: Response):
        if app.state.wechat is None:
            raise GateError(404, "login_disabled", "Not Found")
        app_id, secret, login_limit, source_limit = app.state.wechat
        db_path = app.state.admit[1]
        try:
            credential = LoginCode.model_validate_json(await read_body(request, 2048))
        except ValidationError:
            raise GateError(422, "invalid_login_input", "Invalid login code") from None
        source = login_source(request)
        try:
            retry_after = await run_in_threadpool(reserve_login_exchange, db_path, login_limit, source, source_limit)
        except sqlite3.Error:
            raise GateError(503, "login_limit_unavailable", "Login limit unavailable") from None
        if retry_after is not None:
            raise GateError(429, "local_login_limit", "Login exchange limit reached", {"Retry-After": str(retry_after)})
        try:
            openid = await run_in_threadpool(exchange_code, app_id, secret, credential.code)
        except WeChatLoginBlocked:
            raise GateError(403, "wechat_login_blocked", "WeChat denied login") from None
        except WeChatRateLimited:
            raise GateError(429, "wechat_login_limit", "WeChat login rate limited") from None
        except WeChatUnavailable:
            raise GateError(502, "wechat_unavailable", "WeChat login unavailable") from None
        if openid is None:
            raise GateError(401, "invalid_login_code", "Invalid or expired login code")
        try:
            token = await run_in_threadpool(new_session, db_path, app_id, openid)
        except sqlite3.Error:
            raise GateError(503, "session_store_unavailable", "Session store unavailable") from None
        response.headers.update(NO_STORE)
        return {"access_token": token, "expires_in": SESSION_TTL}

    @app.post(ADMISSION_PATH)
    async def chat(request: Request):
        _, db_path, user_limit, total_limit = app.state.admit
        user = await authenticated_user(request, allow_pilot=True)

        try:
            question = PilotQuestion.model_validate_json(await read_body(request, MAX_BODY_BYTES))
        except ValidationError:
            raise GateError(422, "invalid_chat_input", "Invalid question or conversation history") from None

        try:
            retry_after = await run_in_threadpool(charge_request, db_path, user, user_limit, total_limit)
        except sqlite3.Error:
            raise GateError(503, "chat_quota_unavailable", "Quota check unavailable") from None
        if retry_after is not None:
            raise GateError(429, "chat_quota_reached", "Daily chat quota reached", {"Retry-After": str(retry_after)})

        try:
            upstream = await app.state.upstream.post(CHAT_PATH, json=question.model_dump())
        except httpx.TimeoutException:
            raise GateError(504, "chat_timeout", "Chat service timed out") from None
        except httpx.RequestError:
            raise GateError(502, "chat_unavailable", "Chat service unavailable") from None
        if upstream.status_code != 200 or upstream.headers.get("content-type", "").split(";")[0] != "application/json":
            raise GateError(502, "chat_upstream_failed", "Chat service failed")
        try:
            result = upstream.json()
        except (ValueError, RecursionError):
            raise GateError(502, "invalid_chat_response", "Invalid chat response") from None
        if not (isinstance(result, dict) and isinstance(result.get("answer"), str)
                and isinstance(result.get("sources"), list)):
            raise GateError(502, "invalid_chat_response", "Invalid chat response")
        if not result["answer"].strip():
            request.state.outcome = "empty_answer"
        headers = {**NO_STORE, **{key: upstream.headers[key] for key in ("X-Request-ID", "Server-Timing")
                                  if key in upstream.headers}}
        return Response(upstream.content, media_type="application/json", headers=headers)

    return app
