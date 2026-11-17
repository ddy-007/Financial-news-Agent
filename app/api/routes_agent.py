"""Agent 问答接口。"""
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse
from httpx import TimeoutException
from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger
from openai import APITimeoutError
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from app.agent.chat_sources import ChatSource
from app.agent.react_agent import chat_with_sources

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


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

    headers = {"X-Request-ID": request_id, "Server-Timing": f"chat;dur={elapsed_ms:.1f}"}
    if isinstance(failure, HTTPException):
        failure.headers = {**(failure.headers or {}), **headers}
        raise failure
    if failure is not None:
        return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)
    response.headers.update(headers)
    return result
