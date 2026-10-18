"""文档遗留问题离线回归：筛选、模型解析备用、新闻选取、检索时间、展示与空时间。

仅假模型/假嵌入、内存 SQLite 与临时 Chroma；不联网、不写业务数据库。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.agent.data_agent as DA
import app.agent.experts as E
import app.agent.graph as G
import app.agent.llm as L
import app.agent.tools as T
import app.agent.weekly as W
from app.agent.json_output import extract_json
from app.agent.schemas import RiskOpinion
from app.collectors.news_collector import NewsItem
from app.config import Settings, settings
from app.models.base import Base
from app.models.news import News
from app.models.report import MarketReport
from app.rag.retriever import Hit, HybridRetriever
from app.services.news_selection import distinct_source_count, select_news
from app.services.news_service import _item_to_news

NOW = datetime(2026, 10, 16, 18, 30)


@contextmanager
def fake_chain(outputs, *, cooling=False):
    """真实 get_llm 组装逻辑；只替换网络模型客户端。"""
    calls = []
    clients = []
    for index, output in enumerate(outputs):
        def invoke(prompt, index=index, output=output):
            calls.append(index)
            if isinstance(output, Exception):
                raise output
            return AIMessage(content=output if isinstance(output, str) else json.dumps(output))
        clients.append(RunnableLambda(invoke))
    with ExitStack() as stack:
        stack.enter_context(patch.object(L, "resolve", return_value=("test", "https://example.invalid", "0")))
        stack.enter_context(patch.object(L, "_make_client", return_value=clients[0]))
        stack.enter_context(patch.object(L, "_build_fallbacks", side_effect=lambda *args: clients[1:]))
        stack.enter_context(patch.object(L, "effective_fallback_models", return_value=[str(i) for i in range(1, len(clients))]))
        stack.enter_context(patch.object(L, "_primary_cooling", return_value=NOW if cooling else None))
        yield calls


class DocumentBugfixContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def news(self, title="政策", *, dt=NOW, source="新浪财经", sources=None,
             category="宏观", count=1):
        row = News(title=title, content=title + "正文", source=source, category=category,
                   publish_time=dt, source_count=count,
                   source_urls=json.dumps(sources or [], ensure_ascii=False))
        self.db.add(row)
        self.db.flush()
        return row

    def test_recent_news_filter_precedes_limit(self):
        self.news("央行政策", dt=NOW - timedelta(hours=4))
        for i in range(5):
            self.news(f"行业消息{i}", category="行业", dt=NOW + timedelta(seconds=i))
        self.db.commit()
        with patch.object(T, "SessionLocal", side_effect=lambda: Session(self.engine)):
            for kwargs in ({"keyword": "央行"}, {"category": "宏观"},
                           {"keyword": "央行", "category": "宏观", "limit": 1}):
                self.assertIn("央行政策", T.get_recent_news.func(**kwargs))
            self.assertEqual(T.get_recent_news.func(keyword="不存在"), "暂无新闻。")
            self.assertEqual(len(T.get_recent_news.func(limit=-1).splitlines()), 1)

    def test_json_string_brackets_nested_and_code_fence(self):
        value = {"note": '字符串中的 [ ] { } 和 "引号"', "nested": {"items": [1, 2]}}
        raw = json.dumps(value, ensure_ascii=False)
        for text in (raw, "```json\n" + raw + "\n```", "解释前缀 " + raw + " 结束"):
            self.assertEqual(extract_json(text), value)
        self.assertEqual(DA._extract_json("解释 [1, {\"x\": [2]}] 结束"), [1, {"x": [2]}])

    def test_json_multiple_candidates_rejects_ambiguity(self):
        for text in ('草稿 {"x":1} 最终 {"x":2}', "说明 [1] 最终 [2]"):
            with self.assertRaisesRegex(ValueError, "多个不同 JSON"):
                extract_json(text)
        self.assertIsNone(extract_json("broken json"))
        self.assertIsNone(extract_json(None))
        self.assertIsNone(extract_json("[1]", expected_type=dict))
        self.assertIsNone(extract_json("{}", expected_type=list))
        self.assertEqual(extract_json('{"x":1} 重复 {"x":1}'), {"x": 1})

    def test_malformed_json_outer_does_not_become_nested_report(self):
        self.assertIsNone(extract_json('{"draft":{"score": 0.3}', expected_type=dict))
        self.assertIsNone(extract_json('{"draft":{"score": 0.3},bad}', expected_type=dict))
        self.assertEqual(extract_json('无效 {bad} 最终 {"score":0.2}', expected_type=dict),
                         {"score": 0.2})

    def test_expert_invalid_primary_calls_fallback(self):
        good = {"stance": "看多", "score": 0.3, "confidence": "high", "key_points": ["事实"]}
        for invalid in ("not JSON", "[]", '{"x":1} {"x":2}'):
            with self.subTest(invalid=invalid), fake_chain([invalid, good]) as calls:
                op = E.run_macro_expert(L.get_llm(part="expert"), {}, part="expert")
                self.assertEqual(calls, [0, 1])
                self.assertEqual(op.score, 0.3)

    def test_expert_good_primary_does_not_call_fallback(self):
        with fake_chain([{"score": 0.2}, "[]"]) as calls:
            op = E.run_macro_expert(L.get_llm(part="expert"), {}, part="expert")
        self.assertEqual(calls, [0])
        self.assertEqual(op.score, 0.2)

    def test_all_expert_models_invalid_reports_failure(self):
        with fake_chain(["not JSON", "[]"]) as calls:
            with self.assertRaises(ValueError):
                E.run_macro_expert(L.get_llm(part="expert"), {}, part="expert")
        self.assertEqual(calls, [0, 1])

    def test_cooling_chain_preserves_parsing_and_config(self):
        with fake_chain([{}, "[]", {"score": -0.4}], cooling=True) as calls:
            llm = L.get_llm(part="expert").with_config(tags=["audit"])
            parsed = L.with_output_parser(llm, lambda response: E._require_dict(response.content, "test"))
            self.assertIn("audit", parsed.config["tags"])
            self.assertEqual(parsed.invoke("p")["score"], -0.4)
        self.assertEqual(calls, [1, 2])

    def test_risk_and_chief_parse_fallback_and_preserve_risks(self):
        with fake_chain(["[]", {"risk_level": "低", "risks": ["必须保留"]}]) as calls:
            risk = E.run_risk_officer(L.get_llm(part="expert"), {}, part="expert")
        self.assertEqual(calls, [0, 1])
        quant = {"weighted_score": 0.2, "divergence": 0.1, "divergence_desc": "一致",
                 "final_confidence": "medium", "risk_veto": False}
        with fake_chain(["[]", {"market_summary": "综述", "risks": []}]) as calls:
            report = E.run_chief(L.get_llm(part="expert"), {}, [], risk, quant, part="expert")
        self.assertEqual(calls, [0, 1])
        self.assertIn("必须保留", report["risks"])

    def test_weekly_parse_fallback(self):
        for day in (12, 13):
            self.db.add(MarketReport(date=NOW.replace(day=day), report_day=date(2026, 10, day),
                       report_type="daily", title="日报", content='{"risks":["原有风险"]}', score=0.0))
        self.db.commit()
        with fake_chain(["[]", {"market_summary": "周报", "risks": []}]) as calls:
            report = W.generate_weekly_report(self.db, end_date=date(2026, 10, 16))
        self.assertEqual(calls, [0, 1])
        self.assertEqual(json.loads(report.content)["market_summary"], "周报")
        self.assertIn("原有风险", json.loads(report.content)["risks"])

    def test_weekly_bad_list_fields_use_fallback(self):
        for day in (12, 13):
            self.db.add(MarketReport(date=NOW.replace(day=day), report_day=date(2026, 10, day),
                       report_type="daily", title="日报", content='{"risks":["原有风险"]}', score=0.0))
        self.db.commit()
        for key in ("risks", "key_drivers", "sector_opportunities"):
            with self.subTest(key=key), fake_chain([{key: 1}, {"market_summary": "ok", "risks": []}]) as calls:
                report = W.generate_weekly_report(self.db, end_date=date(2026, 10, 16))
                self.assertEqual(calls, [0, 1])
                self.assertEqual(json.loads(report.content)["market_summary"], "ok")

    def test_fallback_report_parse_switch(self):
        with fake_chain(["[]", {"market_summary": "降级综述"}]) as calls, \
             patch.object(G, "_market_snapshot", return_value=("无", "无")), \
             patch.object(G, "_calendar_degraded", return_value=False):
            report = G._fallback_report(self.db, NOW)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(report["market_summary"], "降级综述")

    def test_realtime_fallback_report_never_uses_future_news(self):
        self.news("截止前事实", dt=datetime.now() - timedelta(hours=1))
        self.news("未来不能使用", dt=datetime.now() + timedelta(days=1))
        self.db.commit()
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content='{"market_summary":"ok"}')
        with patch.object(G, "get_llm", return_value=llm), \
             patch.object(G, "_market_snapshot", return_value=("无", "无")), \
             patch.object(G, "_calendar_degraded", return_value=False):
            G._fallback_report(self.db)
        prompt = llm.invoke.call_args.args[0]
        self.assertIn("截止前事实", prompt)
        self.assertNotIn("未来不能使用", prompt)

    def test_selection_keeps_early_event_and_rejects_future(self):
        important = self.news("早间政策", dt=NOW - timedelta(hours=4), sources=[
            {"source": "财联社"}, {"source": "东方财富"}])
        for i in range(30):
            self.news(f"晚间小消息{i}", dt=NOW - timedelta(minutes=5, seconds=i))
        future = self.news("未来消息", dt=NOW + timedelta(seconds=1))
        self.db.commit()
        rows, stale = G._fetch_news(self.db, ["宏观"], 8, NOW - timedelta(days=1), NOW)
        self.assertIn(important, rows)
        self.assertNotIn(future, rows)
        self.assertEqual(len(rows), 8)
        self.assertFalse(stale)

    def test_selection_covers_sparse_segments_and_refills_quota(self):
        early = self.news("早", dt=NOW - timedelta(hours=20))
        midday = self.news("午", dt=NOW - timedelta(hours=10))
        for i in range(20):
            self.news(str(i), dt=NOW - timedelta(minutes=i))
        rows = select_news(self.db.query(News).all(), 8, NOW - timedelta(days=1), NOW)
        self.assertIn(early, rows)
        self.assertIn(midday, rows)
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({r.id for r in rows}), 8)

    def test_source_count_uses_unique_names_and_ignores_legacy_count(self):
        row = self.news(count=99, sources=[{"source": "新浪财经"}, {"source": "新浪财经"}])
        self.assertEqual(distinct_source_count(row), 1)
        row.source_urls = "invalid"
        self.assertEqual(distinct_source_count(row), 1)
        self.assertIn("（1源）", G._fmt_news([row]))

    def test_rank_weights_are_independent_and_config_validated(self):
        older = self.news("多源", dt=NOW - timedelta(hours=4), sources=[
            {"source": "财联社"}, {"source": "东方财富"}])
        newer = self.news("单源", dt=NOW - timedelta(minutes=5))
        since = NOW - timedelta(days=1)
        with patch.object(settings, "news_selection_segments", 1):
            self.assertEqual(select_news([older, newer], 1, since, NOW), [older])
            with patch.object(settings, "news_source_weight", 0):
                self.assertEqual(select_news([older, newer], 1, since, NOW), [newer])
            with patch.object(settings, "news_recency_gravity", 0):
                self.assertEqual(select_news([older, newer], 1, since, NOW), [older])
        for kwargs in ({"news_per_category": 0}, {"news_source_weight": -1},
                       {"news_recency_gravity": float("nan")}):
            with self.assertRaises(ValueError):
                Settings(_env_file=None, **kwargs)

    def test_selection_stale_fallback_still_excludes_future(self):
        old = self.news("旧", dt=NOW - timedelta(days=3))
        self.news("未来", dt=NOW + timedelta(hours=1))
        self.db.commit()
        rows, stale = G._fetch_news(self.db, ["宏观"], 8, NOW - timedelta(days=1), NOW)
        self.assertEqual(rows, [old])
        self.assertTrue(stale)

    def test_query_time_windows_and_history_remain_available(self):
        day = datetime(2026, 10, 16)
        self.assertEqual(T._search_window("今天政策", now=NOW), (day, NOW))
        self.assertEqual(T._search_window("昨天到今天政策", now=NOW), (day - timedelta(days=1), NOW))
        self.assertEqual(T._search_window("昨天政策", now=NOW),
                         (day - timedelta(days=1), day - timedelta(microseconds=1)))
        self.assertEqual(T._search_window("政策", now=NOW), (NOW - timedelta(days=7), NOW))
        since, until = T._search_window("历史政策", "2026-09-01", "2026-09-30", now=NOW)
        self.assertEqual(since, datetime(2026, 9, 1))
        self.assertEqual(until.date(), date(2026, 9, 30))
        self.assertEqual(T._search_window("2026-09-01政策", now=NOW)[0], datetime(2026, 9, 1))
        with self.assertRaises(ValueError):
            T._search_window("x", "2026-10-17", now=NOW)
        self.assertIn("日期无效", T.search_news.func("x", "invalid"))

    def test_bounded_retrieval_uses_same_candidates_and_metadata(self):
        older = self.news("旧", dt=NOW - timedelta(days=40))
        current = self.news("当前", dt=NOW)
        self.db.commit()
        retriever = HybridRetriever()
        with patch("app.db.SessionLocal", side_effect=lambda: Session(self.engine)), \
             patch.object(retriever, "_dense_search", return_value=[
                 Hit(older.id, "错误旧文", {}), Hit(current.id, "旧索引正文", {})]) as dense, \
             patch("app.rag.retriever.rerank", side_effect=lambda query, texts: [1] * len(texts)):
            hits = retriever.hybrid_search("当前", since=NOW - timedelta(days=1), until=NOW)
        self.assertEqual(dense.call_args.kwargs["ids"], [current.id])
        self.assertEqual([h.id for h in hits], [current.id])
        self.assertEqual(hits[0].text, current.title + "\n" + current.content)
        self.assertEqual(hits[0].metadata["publish_time"], NOW.isoformat())

    def test_history_sparse_recall_works_outside_cached_seven_days(self):
        older = self.news("历史降准", dt=NOW - timedelta(days=40))
        self.news("今天", dt=NOW)
        self.db.commit()
        retriever = HybridRetriever()
        with patch("app.db.SessionLocal", side_effect=lambda: Session(self.engine)), \
             patch.object(retriever, "_dense_search", return_value=[]), \
             patch("app.rag.retriever.rerank", side_effect=lambda query, texts: [1] * len(texts)):
            hits = retriever.hybrid_search("历史降准", since=NOW - timedelta(days=41),
                                            until=NOW - timedelta(days=39))
        self.assertEqual([h.id for h in hits], [older.id])

    def test_empty_window_never_embeds_or_reranks(self):
        retriever = HybridRetriever()
        with patch("app.db.SessionLocal", side_effect=lambda: Session(self.engine)), \
             patch.object(retriever, "_dense_search") as dense:
            self.assertEqual(retriever.hybrid_search("x", since=NOW, until=NOW), [])
            dense.assert_not_called()
        with self.assertRaises(ValueError):
            retriever.hybrid_search("x", since=NOW, until=NOW - timedelta(days=1))

    def test_unbounded_low_level_retrieval_preserves_existing_contract(self):
        retriever = HybridRetriever()
        with patch.object(retriever, "_dense_search", return_value=[Hit("a", "A")]) as dense, \
             patch.object(retriever, "_sparse_search", return_value=[Hit("b", "B")]) as sparse, \
             patch("app.rag.retriever.rerank", return_value=[1, 2]):
            self.assertEqual([h.id for h in retriever.hybrid_search("q")], ["b", "a"])
            dense.assert_called_once_with("q", 20)
            sparse.assert_called_once_with("q", 20)

    def test_real_chroma_id_filter_happens_before_topk(self):
        import chromadb
        with tempfile.TemporaryDirectory() as folder:
            client = chromadb.PersistentClient(path=folder)
            collection = client.get_or_create_collection("contract", metadata={"hnsw:space": "cosine"})
            collection.upsert(ids=["old", "new"], embeddings=[[1.0, 0.0], [0.8, 0.6]],
                              documents=["old", "new"])
            with patch("app.rag.retriever.get_collection", return_value=collection), \
                 patch("app.rag.retriever.embed_query", return_value=[1.0, 0.0]):
                hits = HybridRetriever()._dense_search("q", 1, ids=["new", "not-indexed"])
            self.assertEqual([h.id for h in hits], ["new"])
            client.delete_collection("contract")
            # Chroma 的全局系统缓存持有文件，显式停止测试实例后临时目录才可清理。
            client._system.stop()
            chromadb.api.client.SharedSystemClient.clear_system_cache()

    def test_window_dense_failure_still_returns_sparse_hits(self):
        current = self.news("当前", dt=NOW)
        self.db.commit()
        retriever = HybridRetriever()
        with patch("app.db.SessionLocal", side_effect=lambda: Session(self.engine)), \
             patch.object(retriever, "_dense_search", side_effect=RuntimeError("index unavailable")), \
             patch("app.rag.retriever.rerank", side_effect=lambda query, texts: [1] * len(texts)):
            hits = retriever.hybrid_search("当前", since=NOW - timedelta(days=1), until=NOW)
        self.assertEqual([h.id for h in hits], [current.id])

    def test_fallback_report_bad_numeric_field_uses_backup(self):
        for bad in ("not a number", {"wrong": 1}, float("nan"), float("inf")):
            with self.subTest(bad=bad), fake_chain([{"score": bad}, {"score": 0.3}]) as calls, \
                 patch.object(G, "_market_snapshot", return_value=("无", "无")), \
                 patch.object(G, "_calendar_degraded", return_value=False):
                report = G._fallback_report(self.db, NOW)
                self.assertEqual(report["score"], 0.3)
                self.assertEqual(calls, [0, 1])

    def test_missing_timestamp_stays_unknown_and_cannot_semantically_merge(self):
        item = NewsItem(title="未知日期", source="s", publish_time=None)
        self.assertIsNone(_item_to_news(item).publish_time)
        cand = DA.Candidate(item=item, sources=[("s", None)])
        row = DA._insert_new(self.db, cand, {"category": "综合", "market": "无", "themes": []})
        self.assertIsNone(row.publish_time)
        known = self.news(dt=NOW)
        class Collection:
            def count(self): return 1
            def query(self, **kwargs): return {"ids": [[known.id]], "distances": [[0.0]]}
        with patch.object(DA.vector_store, "get_collection", return_value=Collection()):
            import numpy as np
            self.assertEqual(DA._nearest_existing(self.db, [cand], np.array([[1.0]])), [None])

    def test_risk_display_avoids_duplicate_and_keeps_unlisted_risks(self):
        file = Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py"
        tree = ast.parse(file.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("page_dashboard", "_fmt_score")]
        st = MagicMock()
        st.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
        ns = {"st": st}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(file), "exec"), ns)
        data = {"date": "2026-10-16", "content": {"risks": ["重复风险"], "risk_opinion": {
            "risk_level": "中", "risks": ["重复风险", "未入主清单"],
            "counter_arguments": ["反驳"], "worst_case": "最坏情形"}}}
        ns["page_dashboard"](data, include_market=False)
        lines = [call.args[0] for call in st.markdown.call_args_list]
        self.assertEqual(sum("重复风险" in line for line in lines), 1)
        self.assertTrue(any("未入主清单" in line for line in lines))
        self.assertTrue(any("反驳" in line for line in lines))
        self.assertTrue(any("最坏情形" in line for line in lines))


if __name__ == "__main__":
    from loguru import logger
    logger.remove()
    unittest.main(verbosity=2)
