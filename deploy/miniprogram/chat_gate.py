"""Opt-in admission control for the single-host mini program chat pilot."""

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from time import time as unix_time

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from .wechat_login import WeChatLoginBlocked, WeChatRateLimited, WeChatUnavailable, exchange_code


CHAT_PATH = "/api/v1/agent/chat"
ADMISSION_PATH = "/internal/miniprogram/chat"
LOGIN_PATH = "/internal/miniprogram/wechat-login"
READ_AUTH_PATH = "/internal/miniprogram/wechat-read-auth"
MAX_BODY_BYTES = 64 * 1024
SESSION_TTL = 3600
LOGIN_WINDOW_SECONDS = 60
DEFAULT_LOGIN_LIMIT = 12
BUSINESS_TZ = timezone(timedelta(hours=8))
NO_STORE = {"Cache-Control": "no-store"}
TOKEN_PATTERN = re.compile(r"Bearer ([A-Za-z0-9_-]{43})", re.IGNORECASE | re.ASCII)


class LoginCode(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


async def read_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Request body too large", NO_STORE)
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


def reserve_login_exchange(db_path, limit):
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = unix_time()
        db.execute("DELETE FROM wechat_login_attempts WHERE admitted_at <= ?", (now - LOGIN_WINDOW_SECONDS,))
        count, = db.execute("SELECT COUNT(*) FROM wechat_login_attempts").fetchone()
        if count >= limit:
            expires_from, = db.execute(
                "SELECT admitted_at FROM wechat_login_attempts ORDER BY admitted_at LIMIT 1 OFFSET ?",
                (count - limit,),
            ).fetchone()
            return max(1, math.ceil(expires_from + LOGIN_WINDOW_SECONDS - now))
        db.execute("INSERT INTO wechat_login_attempts VALUES (?)", (now,))
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
            if app_id:
                login_limit = int(os.environ.get("MINIPROGRAM_WECHAT_LOGIN_PER_MINUTE", DEFAULT_LOGIN_LIMIT))
            port = int(os.environ.get("MINIPROGRAM_UPSTREAM_PORT", "8000"))
            if (not db_path.is_absolute() or not 1 <= user_limit <= total_limit
                    or not 1 <= login_limit <= 120 or not 1 <= port <= 65535):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise RuntimeError("Invalid mini program chat pilot configuration") from None

        try:
            with closing(sqlite3.connect(db_path, timeout=2)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, user TEXT, count INTEGER NOT NULL, "
                           "PRIMARY KEY (day, user))")
                db.execute("CREATE TABLE IF NOT EXISTS wechat_sessions (digest TEXT PRIMARY KEY, "
                           "user TEXT NOT NULL, expires_at INTEGER NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS wechat_login_attempts (admitted_at REAL NOT NULL)")
        except sqlite3.Error:
            raise RuntimeError("Cannot initialize mini program chat quota database") from None

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                     timeout=httpx.Timeout(175, connect=3)) as client:
            app.state.admit = (users_by_digest, db_path, user_limit, total_limit)
            app.state.wechat = (app_id, secret, login_limit) if app_id else None
            app.state.upstream = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

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
                    raise HTTPException(503, "Session check unavailable", NO_STORE) from None
                if user:
                    return user
        raise HTTPException(401, "Valid access token required", {**NO_STORE, "WWW-Authenticate": "Bearer"})

    @app.get(READ_AUTH_PATH)
    async def authorize_read(request: Request):
        await authenticated_user(request)
        return Response(status_code=204, headers={**NO_STORE, "X-Miniprogram-Read-Authorized": "1"})

    @app.post(LOGIN_PATH)
    async def login(request: Request, response: Response):
        if app.state.wechat is None:
            raise HTTPException(404, "Not Found", NO_STORE)
        app_id, secret, login_limit = app.state.wechat
        db_path = app.state.admit[1]
        try:
            credential = LoginCode.model_validate_json(await read_body(request, 2048))
        except ValidationError:
            raise HTTPException(422, "Invalid login code", NO_STORE) from None
        try:
            retry_after = await run_in_threadpool(reserve_login_exchange, db_path, login_limit)
        except sqlite3.Error:
            raise HTTPException(503, "Login limit unavailable", NO_STORE) from None
        if retry_after is not None:
            raise HTTPException(429, "Login exchange limit reached", {**NO_STORE, "Retry-After": str(retry_after)})
        try:
            openid = await run_in_threadpool(exchange_code, app_id, secret, credential.code)
        except WeChatLoginBlocked:
            raise HTTPException(403, "WeChat denied login", NO_STORE) from None
        except WeChatRateLimited:
            raise HTTPException(429, "WeChat login rate limited", NO_STORE) from None
        except WeChatUnavailable:
            raise HTTPException(502, "WeChat login unavailable", NO_STORE) from None
        if openid is None:
            raise HTTPException(401, "Invalid or expired login code", NO_STORE)
        try:
            token = await run_in_threadpool(new_session, db_path, app_id, openid)
        except sqlite3.Error:
            raise HTTPException(503, "Session store unavailable", NO_STORE) from None
        response.headers.update(NO_STORE)
        return {"access_token": token, "expires_in": SESSION_TTL}

    @app.post(ADMISSION_PATH)
    async def chat(request: Request):
        _, db_path, user_limit, total_limit = app.state.admit
        user = await authenticated_user(request, allow_pilot=True)

        try:
            question = PilotQuestion.model_validate_json(await read_body(request, MAX_BODY_BYTES))
        except ValidationError:
            raise HTTPException(422, "Invalid question or conversation history", NO_STORE) from None

        try:
            retry_after = await run_in_threadpool(charge_request, db_path, user, user_limit, total_limit)
        except sqlite3.Error:
            raise HTTPException(503, "Quota check unavailable", NO_STORE) from None
        if retry_after is not None:
            raise HTTPException(429, "Daily chat quota reached", {**NO_STORE, "Retry-After": str(retry_after)})

        try:
            upstream = await app.state.upstream.post(CHAT_PATH, json=question.model_dump())
        except httpx.TimeoutException:
            raise HTTPException(504, "Chat service timed out", NO_STORE) from None
        except httpx.RequestError:
            raise HTTPException(502, "Chat service unavailable", NO_STORE) from None
        if upstream.status_code != 200 or upstream.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(502, "Chat service failed", NO_STORE)
        try:
            result = upstream.json()
        except (ValueError, RecursionError):
            raise HTTPException(502, "Invalid chat response", NO_STORE) from None
        if not (isinstance(result, dict) and isinstance(result.get("answer"), str)
                and isinstance(result.get("sources"), list)):
            raise HTTPException(502, "Invalid chat response", NO_STORE)
        headers = {**NO_STORE, **{key: upstream.headers[key] for key in ("X-Request-ID", "Server-Timing")
                                  if key in upstream.headers}}
        return Response(upstream.content, media_type="application/json", headers=headers)

    return app
