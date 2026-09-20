"""交互式问答 Agent（LangChain 1.x 的 create_agent）。"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

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
