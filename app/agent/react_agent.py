"""交互式问答 Agent（LangChain 1.x 的 create_agent）。"""
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent.chat_sources import ChatSource, collect_chat_sources
from app.agent.llm import get_llm
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import ALL_TOOLS

_agent = None


def get_chat_agent():
    """取问答 Agent（惰性构建，进程内复用）。

    **改 `.env` 后必须重启后端才生效。** 这不是本模块的特例——
    `settings` 是 import 时实例化的单例，pydantic-settings 只在那时读一次 `.env`，
    之后改文件或环境变量都不会刷新。**所有环节都一样**（它们虽然每次新建 LLM 对象，
    但读的还是同一个冻结的 `settings`）。

    > 曾试过在这里加「配置指纹」做热更新，但指纹本身也取自那个冻结的 settings，
    > 条件永远为假 —— 是个看着像修好了的空转逻辑，已删除。
    """
    global _agent
    if _agent is None:
        _agent = create_agent(
            get_llm(temperature=0.3, part="chat"),
            ALL_TOOLS,
            system_prompt=SYSTEM_PROMPT,
        )
    return _agent


def chat_with_sources(query: str, chat_history: list[BaseMessage] | None = None) -> tuple[str, list[ChatSource]]:
    """一次调用，同时返回最终文本和本轮新闻工具提供的来源。"""
    agent = get_chat_agent()
    messages = list(chat_history or [])
    messages.append(HumanMessage(content=query))
    initial_count = len(messages)
    result = agent.invoke({"messages": messages})
    msgs = result.get("messages", [])
    if len(msgs) <= initial_count or not isinstance(msgs[-1], AIMessage) or msgs[-1].tool_calls:
        return "", []
    answer = msgs[-1].text
    return answer, collect_chat_sources(msgs[initial_count:]) if answer.strip() else []


def chat(query: str, chat_history: list[BaseMessage] | None = None) -> str:
    """兼容只需要文本回答的调用方。"""
    return chat_with_sources(query, chat_history)[0]
