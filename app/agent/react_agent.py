"""交互式问答 Agent（LangChain 1.x 的 create_agent）。"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from app.agent.llm import get_llm
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import ALL_TOOLS

_agent = None


def get_chat_agent():
    global _agent
    if _agent is None:
        _agent = create_agent(
            get_llm(temperature=0.3),
            ALL_TOOLS,
            system_prompt=SYSTEM_PROMPT,
        )
    return _agent


def chat(query: str, chat_history: list | None = None) -> str:
    """执行一次对话，返回文本回答。chat_history 为 LangChain 消息对象列表。"""
    agent = get_chat_agent()
    messages = list(chat_history or [])
    messages.append(HumanMessage(content=query))
    result = agent.invoke({"messages": messages})
    msgs = result.get("messages", [])
    if not msgs:
        return ""
    content = msgs[-1].content
    if isinstance(content, list):  # 新版 content 可能是 block 列表
        content = "".join(
            part if isinstance(part, str) else getattr(part, "text", str(part))
            for part in content
        )
    return content
