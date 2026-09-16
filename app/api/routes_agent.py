"""Agent 问答接口。"""
from fastapi import APIRouter
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from app.agent.react_agent import chat

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class ChatRequest(BaseModel):
    query: str
    chat_history: list[list[str]] = []  # 形如 [[user, ai], ...]


def _to_history(chat_history: list[list[str]]) -> list:
    msgs = []
    for pair in chat_history:
        if len(pair) >= 2:
            msgs.append(HumanMessage(content=pair[0]))
            msgs.append(AIMessage(content=pair[1]))
    return msgs


@router.post("/chat")
def chat_endpoint(req: ChatRequest):
    answer = chat(req.query, _to_history(req.chat_history))
    return {"answer": answer}
