"""日志巡检问题 1/2/3 的离线回归：假模型、内存 SQLite、前端函数替身。

运行：.venv/Scripts/python.exe -B scripts/check_log_audit_contracts.py
不联网、不加载嵌入模型、不启动应用、不读写业务数据库。
"""
from __future__ import annotations

import ast
import json
import sys
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.agent.data_agent as DA
import app.agent.graph as G
import app.agent.llm as L
from app.agent.experts import run_chief
from app.agent.schemas import RiskOpinion
from app.collectors.news_collector import NewsItem, SourceResult
from app.models.base import Base
from app.models.collector_state import CollectorState
from app.models.news import News
from app.services.collector_health import evaluate, log_health
from app.services.news_service import save_states


def classification(*ids):
    return [{"id": i, "relevant": True, "category": "宏观",
             "market": "A股", "themes": []} for i in ids]


@contextmanager
def fake_models(outputs, *, cooling=False):
    """运行真实备用链，仅替换模型传输。"""
    calls = []

    def model(name, output):
        def invoke(prompt):
            calls.append(name)
            if isinstance(output, Exception):
                raise output
            return AIMessage(content=output if isinstance(output, str) else json.dumps(output))
        return RunnableLambda(invoke)

    clients = [model(str(i), o) for i, o in enumerate(outputs)]
    with ExitStack() as stack:
        stack.enter_context(patch.object(L, "resolve", return_value=("test", "https://example.invalid", "0")))
        stack.enter_context(patch.object(L, "_make_client", return_value=clients[0]))
        stack.enter_context(patch.object(L, "_build_fallbacks", side_effect=lambda *a: clients[1:]))
        stack.enter_context(patch.object(L, "effective_fallback_models", return_value=[str(i) for i in range(1, len(clients))]))
        stack.enter_context(patch.object(L, "_primary_cooling", return_value=datetime.now() if cooling else None))
        stack.enter_context(patch.object(DA, "llm_retry_times", return_value=1))
        yield calls


class AuditContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.now = datetime(2026, 10, 16, 18, 30)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def source(self, *, ok=True, items=True, source="财联社"):
        return SourceResult(source=source,
                            items=[NewsItem(title="快讯", source=source, publish_time=self.now)] if items else [],
                            rejected=[], ok=ok, failed_pages=0 if ok else 1)

    def recent_state(self, source="财联社"):
        st = CollectorState(source=source, last_ts=self.now - timedelta(minutes=2),
                            last_ok_at=self.now - timedelta(minutes=1), empty_streak=0)
        self.db.add(st)
        self.db.commit()
        return st

    def test_invalid_primary_uses_fallback(self):
        bad_outputs = [{}, "not JSON", classification(1), classification(1, 1),
                       classification(1, 3), [False],
                       [{**row, "relevant": "false"} for row in classification(1, 2)],
                       [{**row, "category": 7} for row in classification(1, 2)],
                       [{**row, "themes": {"name": "AI"}} for row in classification(1, 2)],
                       [{**row, "id": True} for row in classification(1, 2)]]
        items = [NewsItem(title=str(i), source="财联社") for i in range(2)]
        for bad in bad_outputs:
            with self.subTest(bad=bad), fake_models([bad, classification(1, 2)]) as calls:
                good, failed = DA.classify_news(items)
                self.assertEqual(calls, ["0", "1"])
                self.assertEqual([r["item"] for r in good], items)
                self.assertEqual(failed, [])

    def test_primary_success_does_not_use_fallback(self):
        with fake_models([classification(1), {}]) as calls:
            good, failed = DA.classify_news([NewsItem(title="快讯", source="财联社")])
        self.assertEqual(calls, ["0"])
        self.assertEqual((len(good), len(failed)), (1, 0))

    def test_all_invalid_freezes_watermark(self):
        st = self.recent_state()
        previous = st.last_ts, st.last_ok_at
        items = [NewsItem(title=str(i), source="财联社") for i in range(2)]
        with fake_models([{}, classification(1, 1)]) as calls:
            good, failed = DA.classify_news(items)
        self.assertEqual(calls, ["0", "1"])
        self.assertEqual(good, [])
        self.assertEqual(failed, items)
        save_states(self.db, [self.source()], len(failed))
        self.assertEqual((st.last_ts, st.last_ok_at), previous)

    def test_cooling_chain_still_validates_each_fallback(self):
        for outputs, expected in [([{}, classification(1)], ["1"]),
                                  ([{}, {}, classification(1)], ["1", "2"])]:
            with self.subTest(outputs=outputs), fake_models(outputs, cooling=True) as calls:
                good, failed = DA.classify_news([NewsItem(title="快讯", source="财联社")])
                self.assertEqual(calls, expected)
                self.assertEqual((len(good), len(failed)), (1, 0))

    def test_without_fallback_parser_still_rejects(self):
        with fake_models([{}]) as calls:
            good, failed = DA.classify_news([NewsItem(title="快讯", source="财联社")])
        self.assertEqual(calls, ["0"])
        self.assertEqual((len(good), len(failed)), (0, 1))

    def test_fetch_failure_is_immediate_and_not_stale(self):
        self.recent_state()
        for has_items, expected in [(True, "partial_failure"), (False, "failed")]:
            with self.subTest(has_items=has_items):
                report = evaluate(self.db, results=[self.source(ok=False, items=has_items)], now=self.now)
                h = report["sources"][0]
                self.assertFalse(report["healthy"])
                self.assertFalse(h["data_stale"])
                self.assertEqual(h["status"], "fetch_failed")
                self.assertEqual(h["execution_status"], expected)
                self.assertEqual(h["failure_stage"], "collection")
                with patch("app.services.collector_health.logger") as logger:
                    log_health(report)
                    logger.warning.assert_called_once()
                    logger.info.assert_not_called()

    def test_classification_failure_is_not_source_failure(self):
        self.recent_state()
        report = evaluate(self.db, results=[self.source()], classify_failed=2, now=self.now)
        h = report["sources"][0]
        self.assertFalse(report["healthy"])
        self.assertEqual(h["status"], "classification_failed")
        self.assertEqual(h["fetch_status"], "success")
        self.assertEqual(h["failure_stage"], "classification")
        self.assertIn("不是源的问题", h["reason"])
        both = evaluate(self.db, results=[self.source(ok=False)], classify_failed=2, now=self.now)
        self.assertIn("采集**不完整**", both["sources"][0]["reason"])
        self.assertNotIn("不是源的问题", both["sources"][0]["reason"])

    def test_zero_new_is_success_and_readonly_is_unknown(self):
        self.recent_state()
        report = evaluate(self.db, results=[self.source(items=False)], now=self.now)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["execution_status"], "success")
        report = evaluate(self.db, now=self.now)
        self.assertEqual(report["execution_status"], "unknown")
        self.assertEqual(report["sources"][0]["fetch_status"], "unknown")

    def test_first_failure_without_state_is_visible(self):
        report = evaluate(self.db, results=[self.source(ok=False, items=False)], now=self.now)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["execution_status"], "failed")

    def freshness(self, **overrides):
        return {"today": "2026-10-16", "today_count": 100, "freshness": "ok", "stale": False,
                "collecting": False, "round_overdue": True,
                "last_round_at": "2026-10-16T14:08:00", "newest_news_at": "2026-10-16T13:59:14",
                "analyzed_at": "2026-10-16T18:30:00", **overrides}

    def chief_prompt(self, fr):
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content="{}")
        quant = {"info_level": {"low_info": True}, "data_freshness": fr,
                 "weighted_score": 0.0, "divergence": 0.0, "divergence_desc": "一致",
                 "final_confidence": "medium", "risk_veto": False}
        run_chief(llm, {}, [], RiskOpinion(), quant, part="expert")
        self.assertEqual(quant["final_confidence"], "medium")
        return llm.invoke.call_args.args[0]

    def test_chief_gets_overdue_collecting_and_never_succeeded(self):
        for fr in [self.freshness(), self.freshness(collecting=True, round_overdue=False),
                   self.freshness(last_round_at=None)]:
            with self.subTest(fr=fr):
                prompt = self.chief_prompt(fr)
                self.assertIn("本窗口新闻尚未完整落地", prompt)
                self.assertIn(fr["newest_news_at"], prompt)
                self.assertIn(fr["last_round_at"] or "从未成功或未记录", prompt)
                self.assertNotIn("请在 market_summary 中如实说明「今日无重大消息」", prompt)
        self.assertNotIn("【采集覆盖提示】", self.chief_prompt(self.freshness(round_overdue=False)))

    def test_historical_prepare_does_not_use_live_readiness(self):
        self.db.add(News(title="旧闻", source="财联社", publish_time=self.now - timedelta(hours=1), category="宏观"))
        self.db.commit()
        with patch.object(G, "SessionLocal", return_value=self.db), \
             patch.object(G, "_market_snapshot", return_value=("无", "无")), \
             patch.object(G, "_safe_extra", side_effect=lambda fn, default, label: default), \
             patch.object(G, "_calendar_degraded", return_value=False), \
             patch("app.services.news_service.news_readiness") as ready:
            result = G.prepare_node({"as_of": self.now.isoformat()})
        ready.assert_not_called()
        fr = result["data_freshness"]
        self.assertEqual(fr["readiness_scope"], "historical_unknown")
        self.assertIsNone(fr["round_overdue"])
        self.assertIsNone(fr["collecting"])
        self.assertEqual(fr["newest_news_at"], (self.now - timedelta(hours=1)).isoformat())
        self.assertIn("该时点的采集执行状态未留存", self.chief_prompt(fr))

    def dashboard_text(self, fr):
        source = Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("page_dashboard", "_fmt_score")]
        st = MagicMock()
        st.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
        namespace = {"st": st}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        namespace["page_dashboard"]({"date": "2026-10-16T18:30:00", "low_info": True,
                                      "content": {"data_freshness": fr}}, include_market=False)
        return "\n".join(str(call.args[0]) for method in (st.warning, st.info, st.write)
                          for call in method.call_args_list)

    def test_frontend_uses_saved_snapshot_and_separate_dimensions(self):
        for fr in [self.freshness(), self.freshness(collecting=True), self.freshness(last_round_at=None)]:
            with self.subTest(fr=fr):
                text = self.dashboard_text(fr)
                self.assertIn("本窗口新闻尚未完整落地", text)
                self.assertIn("当日数据覆盖：ok", text)
                self.assertIn(fr["newest_news_at"], text)
                self.assertNotIn("今日无重大消息", text)
        normal = self.dashboard_text(self.freshness(round_overdue=False))
        self.assertIn("采集轮状态：正常完成", normal)
        historical = self.dashboard_text(self.freshness(round_overdue=None, collecting=None,
                                                       readiness_scope="historical_unknown"))
        self.assertIn("覆盖完整性未知", historical)
        self.assertNotIn("今日无重大消息", historical)


if __name__ == "__main__":
    unittest.main(verbosity=2)
