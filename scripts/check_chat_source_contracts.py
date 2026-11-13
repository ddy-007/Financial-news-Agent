"""Offline verification of structured chat sources using real tool/route contracts."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from loguru import logger
from openai import APITimeoutError
from httpx import Request

from app.agent import react_agent, tools
from app.agent.chat_sources import ChatSource
from app.api import routes_agent
from app.api.routes_agent import router
from app.rag.retriever import Hit
from scripts.summarize_chat_latency import summarize


def tool_call(name, args, call_id):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class ToolCallingFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class ChatSourceContracts(unittest.TestCase):
    def test_route_returns_only_current_tool_records_without_extra_model_calls(self):
        hits = [Hit(id="news-1", text="正文", score=1, metadata={
            "title": "实际检索标题", "publish_time": "2026-11-05T12:00:00",
            "url": "https://example.com/news/1",
        })]
        model = ToolCallingFake(responses=[
            AIMessage(content="", tool_calls=[tool_call("search_news", {"query": "新闻"}, "call-1")]),
            AIMessage(content=[{"type": "text", "text": "回答里编造的标题"}]),
        ])
        agent = create_agent(model, [tools.search_news])
        search = Mock(return_value=hits)
        app = FastAPI()
        app.include_router(router)
        with (TestClient(app) as client,
              patch.object(react_agent, "get_chat_agent", return_value=agent) as get_agent,
              patch.object(tools, "get_retriever", return_value=SimpleNamespace(hybrid_search=search))):
            response = client.post("/api/v1/agent/chat", json={
                "query": "继续", "chat_history": [["旧问题", "旧标题 https://example.com/old"]],
            })
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.headers["X-Request-ID"], r"^[0-9a-f]{32}$")
        self.assertRegex(response.headers["Server-Timing"], r"^chat;dur=\d+\.\d$")
        self.assertEqual(response.json(), {
            "answer": "回答里编造的标题",
            "sources": [{"id": "news-1", "title": "实际检索标题", "published_at": "2026-11-05T12:00:00",
                         "url": "https://example.com/news/1"}],
        })
        search.assert_called_once()
        get_agent.assert_called_once()

    def test_recent_news_artifact_preserves_text_and_filters_unsafe_links(self):
        rows = [SimpleNamespace(id="news-2", source="媒体", category="行业", title="新闻标题",
                                publish_time=datetime(2026, 11, 5, 12), url="javascript:alert(1)")]
        db = Mock()
        db.query.return_value.order_by.return_value.limit.return_value.all.return_value = rows
        with patch.object(tools, "SessionLocal", return_value=db):
            result = tools.get_recent_news.invoke(tool_call("get_recent_news", {}, "call-2"))
        self.assertIn("新闻标题", result.content)
        self.assertEqual(result.artifact[0].published_at, "2026-11-05T12:00:00")
        self.assertIsNone(result.artifact[0].url)
        db.close.assert_called_once()

    def test_failed_tools_and_prior_messages_excluded_and_duplicates_capped(self):
        source = ChatSource(id="duplicate", title="可核查新闻")
        other = ChatSource(id="ignore", title="应忽略")
        historical = ToolMessage(content="旧工具", name="search_news", tool_call_id="old", artifact=[other])

        def invoke(state):
            return {"messages": state["messages"] + [
                ToolMessage(content="失败", name="search_news", tool_call_id="bad", artifact=[other], status="error"),
                ToolMessage(content="文本", name="web_search", tool_call_id="web", artifact=[other]),
                ToolMessage(content="成功", name="search_news", tool_call_id="ok", artifact=[source, source]),
                ToolMessage(content="成功", name="get_recent_news", tool_call_id="more", artifact=[
                    ChatSource(id=str(i), title="新闻") for i in range(15)
                ]),
                AIMessage(content="最终回答"),
            ]}

        with patch.object(react_agent, "get_chat_agent", return_value=SimpleNamespace(invoke=invoke)):
            answer, sources = react_agent.chat_with_sources("问题", [historical])
        self.assertEqual(answer, "最终回答")
        self.assertEqual([item.id for item in sources], ["duplicate", *map(str, range(9))])

    def test_no_news_search_returns_empty_sources(self):
        agent = SimpleNamespace(invoke=lambda state: {"messages": state["messages"] + [AIMessage(content="答复")]})
        with patch.object(react_agent, "get_chat_agent", return_value=agent):
            self.assertEqual(react_agent.chat_with_sources("问题"), ("答复", []))
            self.assertEqual(react_agent.chat("问题"), "答复")

    def test_latency_records_outcomes_without_request_or_error_text(self):
        app = FastAPI()
        app.include_router(router)
        timeout = APITimeoutError(request=Request("POST", "https://example.invalid/private-url"))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "chat.log"
            sink = logger.add(path, format="{message}", filter=lambda record: record["name"] == routes_agent.__name__)
            try:
                with (TestClient(app, raise_server_exceptions=False) as client,
                      patch.object(routes_agent, "chat_with_sources", side_effect=[
                          ("private-answer", []), ("", []), timeout, RuntimeError("private-exception"),
                      ])):
                    responses = [client.post("/api/v1/agent/chat", json={"query": "private-query"}) for _ in range(4)]
            finally:
                logger.remove(sink)

            self.assertEqual([res.status_code for res in responses], [200, 200, 500, 500])
            log = path.read_text(encoding="utf-8")
            self.assertIn(responses[0].headers["X-Request-ID"], log)
            self.assertNotIn("private-", log)
            result = summarize([path, path])  # File and console may contain the same requests.
            self.assertEqual(result["completed_requests"], 4)
            self.assertEqual(result["outcomes"], {"answered": 1, "empty": 1, "error": 2})
            self.assertEqual(result["error_kinds"], {"timeout": 1, "execution": 1})
            self.assertEqual(result["answered_latency_ms"]["p50"], result["answered_latency_ms"]["p95"])

    def test_latency_summary_percentiles_and_empty_input(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "chat.log"
            path.write_text("Unrelated log\n", encoding="utf-8")
            self.assertIsNone(summarize([path])["answered_latency_ms"])
            with path.open("a", encoding="utf-8") as stream:
                for index in range(1, 21):
                    stream.write(f"[chat_request] request_id={index:032x} elapsed_ms={index * 100}.0 "
                                 "outcome=answered error_kind=none\n")
            self.assertEqual(summarize([path])["answered_latency_ms"],
                             {"min": 100.0, "p50": 1000.0, "p95": 1900.0, "max": 2000.0})


if __name__ == "__main__":
    unittest.main()
