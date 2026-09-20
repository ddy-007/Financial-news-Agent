"""交互式问答 Agent（LangChain 1.x 的 create_agent）。"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from app.agent.llm import get_llm, resolve
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import ALL_TOOLS

_agent = None
_agent_cfg: tuple[str, str, str] | None = None


def get_chat_agent():
    """取问答 Agent（惰性构建；**配置变了会自动重建**）。

    其它环节每次调用都新建 LLM，改了 `.env` 立刻生效；这里为了省下重复构建
    做了缓存，但若不检查配置就会「改了配置不生效、必须重启后端」。
    所以额外记一个配置指纹，变了就重建。
    """
    global _agent, _agent_cfg
    cfg = resolve("chat")
    if _agent is None or cfg != _agent_cfg:
        _agent = create_agent(
            get_llm(temperature=0.3, part="chat"),
            ALL_TOOLS,
            system_prompt=SYSTEM_PROMPT,
        )
        _agent_cfg = cfg
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
