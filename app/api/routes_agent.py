"""Agent 问答接口。"""
from fastapi import APIRouter
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

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
def chat_endpoint(req: ChatRequest) -> ChatResponse:
    answer, sources = chat_with_sources(req.query, _to_history(req.chat_history))
    return ChatResponse(answer=answer, sources=sources)
