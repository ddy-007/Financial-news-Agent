"""Critic 协议兼容、脱敏诊断和截图所示前端问题的离线回归。"""
from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from loguru import logger
from openai import BadRequestError

import app.agent.graph as G
import app.agent.llm as L
from app.agent.critic import run_critic
from app.agent.llm_errors import error_details
from app.config import settings

PASS = {"is_passed": True, "flaws": [], "revision_instructions": ""}
FAIL = {"is_passed": False, "flaws": ["summary: 50% 无出处"], "revision_instructions": "删除虚构的50%"}


def completion(body, verdict, *, tool=False):
    message = {"role": "assistant", "content": json.dumps(verdict)}
    if tool:
        message.update(content=None, tool_calls=[{
            "id": "audit", "type": "function", "function": {
                "name": "CriticVerdict", "arguments": json.dumps(verdict),
            },
        }])
    return httpx.Response(200, json={
        "id": "mock", "model": body["model"], "choices": [{
            "index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": message,
        }],
    })


class RecoveryContracts(unittest.TestCase):
    def call(self, transport, *, backup=False):
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            def model(name):
                return L._FallbackAwareChatOpenAI(
                    api_key="synthetic", base_url="https://mock.invalid/v1", model=name,
                    http_client=client, max_retries=0,
                )
            llm = model("primary")
            if backup:
                llm = llm.with_fallbacks([model("backup")], exceptions_to_handle=L._FALLBACK_EXCEPTIONS)
            return run_critic(llm, {"news_fulltext": "原文亏损10亿元"}, {"market_summary": "利润率50%"})

    def test_thinking_mode_accepts_auto_tool_choice_and_keeps_rejection(self):
        requests = []

        def transport(request):
            body = json.loads(request.content)
            requests.append(body)
            if body.get("tool_choice") != "auto":
                return httpx.Response(400, json={"error": {
                    "code": "invalid_request_error", "message": "Thinking mode does not support this tool_choice",
                }})
            self.assertEqual(body["tools"][0]["function"]["name"], "CriticVerdict")
            self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
            self.assertIn("严苛的金融风控", body["messages"][0]["content"])
            self.assertIn("必须调用 CriticVerdict", body["messages"][0]["content"])
            self.assertIn("原文亏损10亿元", body["messages"][1]["content"])
            return completion(body, FAIL, tool=True)

        verdict = self.call(transport)
        self.assertFalse(verdict.is_passed)
        self.assertEqual(verdict.flaws, FAIL["flaws"])
        self.assertEqual(len(requests), 1)

    def test_generic_400_and_context_overflow_do_not_retry_or_switch(self):
        for error in (
            {"message": "Invalid schema for tools", "param": "tools", "code": "invalid_schema"},
            {"message": "context_length_exceeded", "code": "context_length_exceeded"},
            {"message": "temperature is not supported", "param": "temperature", "code": "unsupported_parameter"},
        ):
            requests = []

            def transport(request):
                requests.append(json.loads(request.content))
                return httpx.Response(400, json={"error": error})

            with self.subTest(error=error), self.assertRaises(Exception):
                self.call(transport, backup=True)
            self.assertEqual(len(requests), 1)

    def test_invalid_output_switches_to_validated_backup(self):
        requests = []

        def transport(request):
            body = json.loads(request.content)
            requests.append(body["model"])
            if body["model"] == "backup":
                return completion(body, PASS, tool=True)
            return completion(body, dict(PASS, flaws=["不能矛盾通过"]), tool=True)

        self.assertTrue(self.call(transport, backup=True).is_passed)
        self.assertEqual(requests, ["primary", "backup"])

    def test_no_tool_call_is_not_a_pass_and_does_not_loop(self):
        requests = []

        def transport(request):
            requests.append(json.loads(request.content))
            return completion(requests[-1], PASS)  # 普通正文 JSON 不算工具审计结果。

        with self.assertRaises(ValueError):
            self.call(transport)
        self.assertEqual(len(requests), 1)

    def test_native_structured_output_keeps_callbacks_and_binding(self):
        starts = []

        class Recorder(BaseCallbackHandler):
            def on_chat_model_start(self, serialized, messages, **kwargs):
                starts.append(kwargs)

        def transport(request):
            body = json.loads(request.content)
            self.assertEqual(body["max_completion_tokens"], 512)
            self.assertEqual(body["tool_choice"], "auto")
            return completion(body, PASS, tool=True)

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            model = L._FallbackAwareChatOpenAI(api_key="synthetic", model="primary",
                base_url="https://mock.invalid/v1", http_client=client, max_retries=0)
            bound = model.bind(max_tokens=512).with_config(tags=["audit"], callbacks=[Recorder()])
            self.assertTrue(run_critic(bound, {"news_fulltext": "全文"}, {}).is_passed)
        self.assertEqual(len(starts), 1)
        self.assertTrue(all("audit" in x["tags"] for x in starts))

    def test_diagnostics_redact_keys_and_keep_error_and_request_size(self):
        messages = []
        sink = logger.add(lambda message: messages.append(str(message)))
        self.addCleanup(lambda: logger.remove(sink))
        with patch.object(settings, "expert_api_key", "synthetic-secret"):
            exc = BadRequestError("request failed", response=httpx.Response(400,
                request=httpx.Request("POST", "https://mock.invalid/v1")), body={"error": {
                "code": "unsupported_parameter", "param": "tool_choice",
                "message": "tool_choice not supported api_key=synthetic-secret Bearer second-secret https://private.invalid?key=third-secret",
            }})
            details = error_details(exc)
            for value in ("synthetic-secret", "second-secret", "third-secret", "private.invalid"):
                self.assertNotIn(value, json.dumps(details))
            self.assertEqual(details["status_code"], 400)
            with patch.object(G, "get_llm", return_value=object()), patch.object(G, "run_critic", side_effect=exc):
                final = G.critic_node({"opinions": [{}], "ctx": {"news_fulltext": "全文"}, "final": {}})["final"]
            self.assertFalse(final["is_passed"])
            self.assertEqual(final["critic_error"]["param"], "tool_choice")
            self.assertNotIn("synthetic-secret", "".join(messages))
        self.assertEqual(error_details(ValueError("private-output"))["message"], "")

    def test_request_size_is_logged_without_source_or_draft_text(self):
        messages = []
        sink = logger.add(lambda message: messages.append(str(message)))
        try:
            self.assertTrue(self.call(lambda request: completion(json.loads(request.content), PASS, tool=True)).is_passed)
        finally:
            logger.remove(sink)
        log = "".join(messages)
        for field in ("messages=2", "evidence_chars=", "draft_chars=", "message_utf8_bytes="):
            self.assertIn(field, log)
        self.assertNotIn("原文亏损10亿元", log)
        self.assertNotIn("利润率50%", log)

    def test_diagnostics_strip_configured_key_before_redaction(self):
        with patch.object(settings, "expert_api_key", "  synthetic-no-prefix-token\t"):
            exc = BadRequestError("invalid request", response=httpx.Response(400,
                request=httpx.Request("POST", "https://mock.invalid/v1")),
                body={"message": "account synthetic-no-prefix-token rejected this request"})
            self.assertNotIn("synthetic-no-prefix-token", json.dumps(error_details(exc)))


class FrontendContracts(unittest.TestCase):
    def setUp(self):
        source = Path("frontend/streamlit_app.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("page_dashboard", "_fmt_score", "main")]
        self.st = MagicMock()
        self.st.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
        self.ns = {"st": self.st, "get_api_base": lambda: "http://mock.invalid",
                   "page_weekly": MagicMock(), "page_news": MagicMock(), "page_market": MagicMock(),
                   "page_chat": MagicMock(), "page_history": MagicMock()}
        self.st.sidebar.button.return_value = False
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), self.ns)

    def render(self, status):
        self.ns["page_dashboard"]({"content": {"is_passed": False, "critic_status": status,
            "critic_feedback": "批注", "data_freshness": {"evidence_excluded_count": 40}},
            "confidence": "medium"}, include_market=False)

    def test_single_navigation_registers_six_chinese_callable_pages(self):
        self.ns["main"]()
        self.st.sidebar.radio.assert_not_called()
        self.st.navigation.assert_called_once()
        self.assertEqual([c.kwargs["title"] for c in self.st.Page.call_args_list],
                         ["每日研判", "周度综述", "新闻流", "行情", "AI 问答", "历史与回测"])
        self.assertTrue(all(callable(c.args[0]) for c in self.st.Page.call_args_list))
        self.st.navigation.return_value.run.assert_called_once()

    def test_call_failure_is_not_displayed_as_content_rejection(self):
        self.render("error")
        self.st.error.assert_not_called()
        text = "\n".join(c.args[0] for c in self.st.warning.call_args_list)
        self.assertIn("审计调用失败", text)
        self.assertNotIn("状态：error", text)
        self.assertNotIn("报告仅核验", text)
        self.assertIn("分析范围仅涵盖", text)

    def test_content_rejection_remains_an_error(self):
        self.render("rejected")
        self.assertIn("内容被驳回", self.st.error.call_args.args[0])

    def test_missing_evidence_and_unreviewed_have_distinct_messages(self):
        for status, message in (("incomplete_sources", "原文证据不完整"),
                                ("not_reviewed", "尚未复审"), ("skipped", "缺少分析师底稿")):
            self.st.reset_mock()
            self.render(status)
            self.assertIn(message, self.st.warning.call_args_list[0].args[0])

    def test_streamlit_entry_renders_without_errors_or_duplicate_radio(self):
        from streamlit.testing.v1 import AppTest

        def fake_get(client, url, **kwargs):
            report = {"title": "离线测试日报", "confidence": "medium", "score": -0.25, "divergence": 0.35,
                      "content": {"is_passed": False, "critic_status": "error"}}
            return httpx.Response(200, json=report if str(url).endswith("/reports/today") else [],
                                  request=httpx.Request("GET", url))

        with patch.object(httpx.Client, "get", fake_get):
            at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "frontend/streamlit_app.py"),
                                   default_timeout=20).run()
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(len(at.sidebar.radio), 0)
        self.assertEqual(len(at.error), 0)
        self.assertIn("审计调用失败", at.warning[0].value)
        self.assertIn(("置信度", "中"), [(x.label, x.value) for x in at.metric])
        self.assertTrue(any("最终置信度：**中**" in x.value for x in at.info))


if __name__ == "__main__":
    logger.remove()
    unittest.main()
