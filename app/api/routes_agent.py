"""Agent 问答接口。"""
from pathlib import Path
from time import perf_counter
from traceback import walk_tb
from uuid import uuid4

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse
from httpx import TimeoutException
from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException

from app.agent.chat_sources import ChatSource
from app.agent.llm import InsufficientBalanceError
from app.agent.react_agent import chat_with_sources

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])
APP_DIR = Path(__file__).resolve().parents[1]
PROVIDER_ERROR_CATEGORIES = {
    401: "provider_auth",
    402: "provider_payment_required",
    403: "provider_permission",
    429: "provider_rate_limit",
}


class ChatRequest(BaseModel):
    query: str
    chat_history: list[list[str]] = Field(default_factory=list)  # 形如 [[user, ai], ...]


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]


def _to_history(chat_history: list[list[str]]) -> list:
    msgs = []
    for pair in chat_history:
        if len(pair) >= 2:
            msgs.append(HumanMessage(content=pair[0]))
            msgs.append(AIMessage(content=pair[1]))
    return msgs


def _log_chat_error(request_id: str, error_kind: str, error: Exception) -> None:
    category, status = error_kind, "-"
    if isinstance(error, (APIStatusError, HTTPException, InsufficientBalanceError)) and type(error.status_code) is int:
        if 400 <= error.status_code <= 599:
            status = error.status_code
    if isinstance(error, InsufficientBalanceError):
        category = "provider_payment_required"
    elif isinstance(error, APIStatusError):
        category = PROVIDER_ERROR_CATEGORIES.get(status, "provider_http")
        if error.code == "insufficient_quota":
            category = "provider_quota"
    elif isinstance(error, HTTPException):
        category = "http_exception"
    elif isinstance(error, ValidationError):
        category = "validation"
    elif error_kind != "timeout" and isinstance(error, APIConnectionError):
        category = "provider_connection"

    location = "-"
    for frame, line in walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.is_relative_to(APP_DIR):
            location = f"app/{path.relative_to(APP_DIR).as_posix()}:{line}"
    logger.warning(
        "[chat_error] request_id={} category={} status_code={} location={}",
        request_id, category, status, location,
    )


@router.post("/chat", response_model=ChatResponse)
def chat_endpoint(req: ChatRequest, response: Response) -> ChatResponse | Response:
    request_id = uuid4().hex
    started = perf_counter()
    outcome = "error"
    error_kind = "none"
    failure = None
    try:
        answer, sources = chat_with_sources(req.query, _to_history(req.chat_history))
        result = ChatResponse(answer=answer, sources=sources)
        outcome = "answered" if result.answer.strip() else "empty"
    except Exception as exc:
        failure = exc
        error_kind = "timeout" if isinstance(exc, (TimeoutError, TimeoutException, APITimeoutError)) else "execution"
    finally:
        elapsed_ms = (perf_counter() - started) * 1000
        # Only include service-generated values. Request content and provider errors can contain secrets.
        logger.info(
            "[chat_request] request_id={} elapsed_ms={:.1f} outcome={} error_kind={}",
            request_id, elapsed_ms, outcome, error_kind,
        )

    headers = {"x-request-id": request_id, "server-timing": f"chat;dur={elapsed_ms:.1f}"}
    if failure is not None:
        _log_chat_error(request_id, error_kind, failure)
        if isinstance(failure, HTTPException):
            failure.headers = {
                **{name: value for name, value in (failure.headers or {}).items() if name.lower() not in headers},
                **headers,
            }
            raise failure
        return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)
    response.headers.update(headers)
    return result
