"""Modular Streamlit regression checks; synthetic HTTP only, no live services.

Run: .venv/Scripts/python.exe -B scripts/check_frontend_contracts.py
"""
from __future__ import annotations

import ast
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from frontend import api_client as API


REPORT = {
    "id": "offline-daily", "date": "2026-11-01T18:30:00", "report_day": "2026-11-01",
    "title": "离线测试日报", "sentiment": "偏空", "confidence": "medium", "score": -0.25,
    "divergence": 0.35, "low_info": True,
    "expert_opinions": [{"expert": "宏观", "stance": "看空", "score": -0.25,
                         "confidence": "low", "key_points": ["测试观点"]}],
    "content": {"is_passed": False, "critic_status": "incomplete_sources",
                "critic_feedback": "缺少原文", "risks": ["重复风险"],
                "risk_opinion": {"risk_level": "高", "risks": ["重复风险", "额外风险"]},
                "data_freshness": {"evidence_excluded_count": 3}},
}
PAGE_TITLES = ["每日研判", "周度综述", "新闻流", "行情", "AI 问答", "历史与回测"]


def fake_get(client, url, **kwargs):
    path = httpx.URL(url).path
    fixtures = {
        "/api/v1/reports/today": REPORT,
        "/api/v1/reports": [REPORT],
        "/api/v1/reports/backtest": {"total": 1, "correct": 1, "accuracy": 100},
        "/api/v1/reports/weekly": {"title": "测试周报", "score": -0.2,
            "content": {"week_start": "2026-10-26", "week_end": "2026-11-01",
                "daily_count": 1, "market_summary": "测试周报正文",
                "daily_series": [{"date": "2026-11-01", "score": -0.25}]}},
        "/api/v1/market": [{"symbol": "sh000001", "name": "上证指数", "close": 3500,
                            "change_pct": -1.2, "date": "2026-10-30"}],
        "/api/v1/sectors": {"date": "2026-10-30", "rows": [
            {"name": "测试板块", "change_pct": -1.5}]},
        "/api/v1/news/dates": {"dates": [{"date": "2026-11-01", "count": 301}]},
    }
    if path == "/api/v1/news":
        params = kwargs.get("params") or {}
        offset, limit = params.get("offset", 0), params.get("limit", 300)
        body = [{"title": f"测试新闻{i}", "publish_time": "2026-11-01T12:00:00",
                 "source_count": 2, "themes": '["测试"]', "category": "宏观",
                 "market": "A股", "url": f"https://example.invalid/{i}"}
                for i in range(offset, min(offset + limit, 301))]
    else:
        if path not in fixtures:
            raise AssertionError(f"Unexpected test GET: {path}")
        body = fixtures[path]
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


class NavigationContracts(unittest.TestCase):
    def setUp(self):
        API.clear_all_caches()

    def app(self, entry="app.py"):
        return AppTest.from_file(str(ROOT / "frontend" / entry), default_timeout=20).run()

    def switch(self, app, title):
        # AppTest.switch_page only supports file pages, not callable st.Pages.
        matches = [key for key, info in app._registered_pages.items() if info["page_name"] == title]
        self.assertEqual(len(matches), 1, app._registered_pages)
        app._page_hash = matches[0]
        return app.run()

    def test_all_frontend_modules_parse(self):
        for path in (ROOT / "frontend").rglob("*.py"):
            with self.subTest(file=path.name):
                ast.parse(path.read_text(encoding="utf-8"), str(path))

    def test_both_entries_register_only_six_chinese_pages(self):
        with patch.object(httpx.Client, "get", fake_get), \
             patch.object(httpx.Client, "post", side_effect=AssertionError("Unexpected POST")):
            for entry in ("app.py", "streamlit_app.py"):
                with self.subTest(entry=entry):
                    app = self.app(entry)
                    self.assertFalse(app.exception)
                    self.assertFalse(app.sidebar.radio)
                    self.assertEqual([p["page_name"] for p in app._registered_pages.values()], PAGE_TITLES)
                    self.assertFalse(app.error)
                    self.assertIn("原文证据不完整", app.warning[0].value)
                    self.assertIn(("置信度", "中"), [(m.label, m.value) for m in app.metric])
                    warnings = "\n".join(w.value for w in app.warning)
                    self.assertNotIn("报告仅核验", warnings)
                    self.assertTrue(any("不能据此判断" in i.value for i in app.info))

    def test_six_callable_pages_render_without_blank_pages(self):
        with patch.object(httpx.Client, "get", fake_get), \
             patch.object(httpx.Client, "post", side_effect=AssertionError("Unexpected POST")):
            app = self.app()
            for title in PAGE_TITLES:
                with self.subTest(page=title):
                    self.switch(app, title)
                    self.assertFalse(app.exception)
                    self.assertFalse(app.error)
                    self.assertEqual(len(app.main.title), 1)

    def test_news_pagination_preserves_order(self):
        with patch.object(httpx.Client, "get", fake_get):
            app = self.switch(self.app(), "新闻流")
            self.assertEqual(len(app.dataframe[0].value), 300)
            next(b for b in app.button if b.label == "下一页").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.dataframe[0].value["序号"].tolist(), [301])
            next(b for b in app.button if b.label == "上一页").click().run()
            self.assertEqual(app.dataframe[0].value["序号"].tolist(), list(range(1, 301)))

    def test_refresh_reloads_cached_data(self):
        calls = []

        def recorded_get(client, url, **kwargs):
            calls.append(str(url))
            return fake_get(client, url, **kwargs)

        with patch.object(httpx.Client, "get", recorded_get):
            app = self.app()
            first_count = len(calls)
            self.assertGreater(first_count, 0)
            app.run()
            self.assertEqual(len(calls), first_count)
            next(b for b in app.sidebar.button if b.label == "刷新数据").click().run()
            self.assertFalse(app.exception)
            self.assertGreater(len(calls), first_count)

    def test_history_detail_uses_same_audit_and_risk_display(self):
        with patch.object(httpx.Client, "get", fake_get):
            app = self.switch(self.app(), "历史与回测")
            app.selectbox[0].select("offline-daily").run()
            self.assertFalse(app.exception)
            self.assertIn("原文证据不完整", app.warning[0].value)
            lines = [m.value for m in app.markdown]
            self.assertEqual(sum("重复风险" in line for line in lines), 1)
            self.assertTrue(any("额外风险" in line for line in lines))

    def test_null_report_and_expert_confidence_show_placeholders(self):
        expert = {**REPORT["expert_opinions"][0], "confidence": None}
        report = {**REPORT, "confidence": None, "expert_opinions": [expert]}

        def null_get(client, url, **kwargs):
            if httpx.URL(url).path == "/api/v1/reports/today":
                return httpx.Response(200, json=report, request=httpx.Request("GET", url))
            return fake_get(client, url, **kwargs)

        with patch.object(httpx.Client, "get", null_get):
            app = self.app()
            self.assertFalse(app.exception)
            self.assertIn(("置信度", "—"), [(m.label, m.value) for m in app.metric])
            self.assertTrue(any(c.value == "置信度：—" for c in app.caption))
            self.assertTrue(any("最终置信度：**—**" in i.value for i in app.info))

    def test_chat_sends_paired_history_without_live_model(self):
        def fake_post(url, **kwargs):
            return httpx.Response(200, json={"answer": "测试回答"}, request=httpx.Request("POST", url))

        with patch.object(httpx.Client, "get", fake_get), \
             patch.object(httpx.Client, "post", side_effect=fake_post) as post:
            app = self.switch(self.app(), "AI 问答")
            app.chat_input[0].set_value("第一问").run()
            app.chat_input[0].set_value("第二问").run()
            self.assertFalse(app.exception)
            self.assertEqual(post.call_args.kwargs["json"], {
                "query": "第二问", "chat_history": [["第一问", "测试回答"]]})


class ApiContracts(unittest.TestCase):
    def test_cache_routes_and_ttls(self):
        for path, fn, ttl in [
            ("/api/v1/market", API._cached_get_market, 30),
            ("/api/v1/sectors", API._cached_get_market, 30),
            ("/api/v1/news/dates", API._cached_get_default, 60),
            ("/api/v1/reports/today", API._cached_get_report, 300),
        ]:
            with self.subTest(path=path):
                self.assertIs(API._select_cache_fn(path), fn)
                self.assertEqual(fn._info.ttl, ttl)

    def test_post_invalidates_all_caches_but_chat_keeps_them(self):
        client = MagicMock()
        client.post.return_value = httpx.Response(200, json={"ok": True},
            request=httpx.Request("POST", "https://example.invalid"))
        with patch.object(API, "_http_client", return_value=client), \
             patch.object(API, "get_api_base", return_value="https://example.invalid"), \
             patch.object(API._cached_get_default, "clear") as default, \
             patch.object(API._cached_get_market, "clear") as market, \
             patch.object(API._cached_get_report, "clear") as report:
            API.api_post("/api/v1/market/collect")
            for cache in (default, market, report):
                cache.assert_called_once()
                cache.reset_mock()
            API.api_post("/api/v1/agent/chat", invalidate_cache=False)
            for cache in (default, market, report):
                cache.assert_not_called()

    def test_get_and_post_failures_show_distinct_errors(self):
        request = httpx.Request("GET", "https://example.invalid")
        errors = [
            (httpx.HTTPStatusError("bad status", request=request,
                response=httpx.Response(503, request=request)), "HTTP 503"),
            (httpx.ConnectError("connection failed"), "无法连接后端服务"),
            (httpx.ReadTimeout("timeout"), "超时"),
            (ValueError("bad JSON"), "暂时"),
        ]
        for exc, message in errors:
            for method in ("get", "post"):
                with self.subTest(error=type(exc).__name__, method=method), \
                     patch.object(API, "st") as ui, \
                     patch.object(API, "get_api_base", return_value="https://example.invalid"), \
                     patch.object(API, "_select_cache_fn", return_value=MagicMock(side_effect=exc)), \
                     patch.object(API, "_http_client") as client:
                    client.return_value.post.side_effect = exc
                    result = API.api_get("/offline") if method == "get" else API.api_post("/offline")
                    self.assertIsNone(result)
                    self.assertIn(message, ui.error.call_args.args[0])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
