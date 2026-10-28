"""日报链路修复：假采集/假模型、内存 SQLite 和内存日志，不写实库、不发网络请求。"""
from __future__ import annotations

import ast
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.agent.critic as C
import app.agent.experts as E
import app.agent.graph as G
import app.collectors.scheduler as S
import app.collectors.trading_calendar as T
import app.services.market_service as M
import app.services.report_service as R
from app.models.base import Base
from app.agent.schemas import RiskOpinion
from app.models.market import MarketData
from app.models.news import News
from app.models.news_ingest import RawNews

NOW = datetime(2026, 10, 28, 18, 30)


class DatabaseContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def save(self, passed, text):
        return R.upsert_daily_report(
            self.db, NOW.date(), date=NOW, title=text,
            content=json.dumps({"is_passed": passed, "market_summary": text}), score=0.3,
        )

    def test_two_failed_drafts_choose_new_without_changing_identity(self):
        first = self.save(False, "旧失败稿")
        original_id = first.id
        second = self.save(False, "更完整的新失败稿")
        self.assertEqual(second.id, original_id)
        self.assertEqual(second.title, "更完整的新失败稿")
        self.assertEqual(self.db.info["daily_report_write"]["action"], "updated_failed")
        self.assertEqual(self.db.query(type(first)).count(), 1)

    def test_failed_rerun_still_preserves_success(self):
        first = self.save(True, "已通过")
        saved = first.content
        kept = self.save(False, "新失败")
        self.assertEqual(kept.content, saved)
        self.assertEqual(self.db.info["daily_report_write"]["action"], "kept_existing")

    def test_success_after_failed_update_recovers_same_row(self):
        row = self.save(False, "失败1")
        original_id = row.id
        self.save(False, "失败2")
        result = self.save(True, "恢复成功")
        self.assertEqual(result.id, original_id)
        self.assertEqual(result.title, "恢复成功")

    def test_conditional_update_does_not_overwrite_new_concurrent_winner(self):
        report = SimpleNamespace(id="row", date=NOW, content='{"is_passed":false}')
        db = MagicMock(info={})
        db.execute.return_value.rowcount = 0
        def refresh(row):
            if db.refresh.call_count > 1:
                row.content = '{"is_passed":true}'
        db.refresh.side_effect = refresh
        changed = R._apply_report_update(db, report, {"content": '{"is_passed":false,"new":true}'}, True)
        self.assertFalse(changed)
        self.assertTrue(json.loads(report.content)["is_passed"])
        db.rollback.assert_called_once()
        self.assertEqual(db.info["daily_report_write"]["action"], "kept_existing")
        sql = str(db.execute.call_args.args[0])
        self.assertIn("report.content =", sql)

    def test_unique_conflict_with_failed_winner_can_update(self):
        report = SimpleNamespace(id="row", date=NOW, content='{"is_passed":false}')
        db = MagicMock(info={})
        db.query.return_value.filter.return_value.first.side_effect = [None, report]
        db.commit.side_effect = [IntegrityError("insert", {}, Exception("unique")), None]
        db.execute.return_value.rowcount = 1
        result = R.upsert_daily_report(db, NOW.date(), content='{"is_passed":false,"new":true}')
        self.assertIs(result, report)
        self.assertEqual(db.commit.call_count, 2)
        self.assertEqual(db.info["daily_report_write"]["action"], "updated_failed")

    def test_market_readback_detects_post_commit_loss(self):
        def remove_after_commit(session):
            with self.engine.begin() as connection:
                connection.execute(MarketData.__table__.delete())
        event.listen(self.db, "after_commit", remove_after_commit)
        with self.assertRaisesRegex(RuntimeError, "提交后回读缺少"):
            M.save_market_data(self.db, [{"symbol": "sh000001", "date": NOW}])

    def test_market_readback_normal_and_duplicate_are_idempotent(self):
        rows = [{"symbol": "sh000001", "date": NOW}]
        self.assertEqual(M.save_market_data(self.db, rows), 1)
        self.assertEqual(M.save_market_data(self.db, rows), 0)

    def test_market_history_checks_four_a_share_symbols_and_separate_us_dates(self):
        days = [date(2026, 10, 26), date(2026, 10, 27)]
        rows = [{"symbol": symbol, "date": datetime.combine(day, datetime.min.time())}
                for day in days for symbol, _ in M.A_SHARE_INDICES]
        # 美国自身最新交易日可以与 A 股不同，不能因此要求同一天有七个 symbol。
        rows += [{"symbol": symbol, "date": datetime(2026, 10, 23, hour)}
                 for hour, (symbol, _) in enumerate(M.US_INDICES)]
        M.save_market_data(self.db, rows)
        with patch.object(T, "recent_trading_days", return_value=days):
            result = M.check_market_coverage(self.db, NOW.date())
        self.assertEqual(result["history_status"], "complete")
        self.assertEqual(result["us_missing_latest"], [])
        self.db.query(MarketData).filter_by(symbol="sh000001", date=datetime(2026, 10, 26)).delete()
        self.db.commit()
        with patch.object(T, "recent_trading_days", return_value=days):
            result = M.check_market_coverage(self.db, NOW.date())
        self.assertEqual(result["a_share_gaps"], {"2026-10-26": ["sh000001"]})

    def test_unknown_calendar_does_not_claim_complete(self):
        with patch.object(T, "recent_trading_days", return_value=None):
            self.assertEqual(M.check_market_coverage(self.db)["history_status"], "unknown")

    def make_news(self, key, body, *, source="财联社", url=None):
        url = url or f"https://mock.invalid/{key}"
        row = News(id=key, title="原始标题" + key, content=body, source=source, url=url,
                   publish_time=NOW, category="宏观", dedup_policy=C.PAIRWISE_POLICY)
        self.db.add(row)
        self.db.add(RawNews(id="raw" + key, news_id=key, source=source, publish_time=NOW,
                           payload=json.dumps({"title": row.title, "content": body, "source": source,
                                               "url": url, "publish_time": NOW.isoformat()})))
        self.db.commit()
        return row

    def test_representative_with_its_own_url_can_borrow_another_display_url(self):
        row = self.make_news("one", "公司亏损", url="https://mock.invalid/cls")
        row.url = "https://mock.invalid/eastmoney"
        row.source_urls = json.dumps([{"source": "财联社", "url": "https://mock.invalid/cls"},
                                      {"source": "东方财富", "url": row.url},
                                      {"source": "财联社", "url": row.url}])
        self.db.add(RawNews(id="other", news_id=row.id, source="东方财富", publish_time=NOW,
                           payload=json.dumps({"title": "另一标题", "content": "公司亏损",
                                               "source": "东方财富", "url": row.url,
                                               "publish_time": NOW.isoformat()})))
        self.db.commit()
        _, issues = C.load_news_evidence(self.db, [row], NOW)
        self.assertEqual(issues, [])

    def test_budget_excludes_whole_articles_without_false_missing_archive(self):
        one = self.make_news("one", "新闻全文甲" * 60)
        two = self.make_news("two", "新闻全文乙" * 60)
        first, _ = C.load_news_evidence(self.db, [one], NOW)
        with patch.object(C, "MAX_EVIDENCE_CHARS", len(first) + 1):
            ids, fulltext, issues, excluded = C.select_news_evidence(self.db, [[one], [two]], NOW)
        self.assertEqual(ids, {one.id})
        self.assertIn(one.content, fulltext)
        self.assertNotIn(two.content, fulltext)
        self.assertEqual(issues, [])
        self.assertEqual(excluded, 1)

    def test_budget_selection_still_fails_closed_on_real_missing_source(self):
        row = self.make_news("one", "新闻全文")
        self.db.query(RawNews).delete()
        self.db.commit()
        _, _, issues, _ = C.select_news_evidence(self.db, [[row]], NOW)
        self.assertTrue(any("缺少归档全文" in issue for issue in issues))

    def test_prepare_only_distributes_budgeted_news_to_every_role(self):
        rows = [self.make_news(str(i), f"完整新闻{i}" * 60) for i in range(3)]
        first, _ = C.load_news_evidence(self.db, [rows[0]], NOW)
        with patch.object(C, "MAX_EVIDENCE_CHARS", len(first) + 1), \
             patch.object(G, "SessionLocal", return_value=self.db), \
             patch.object(G, "_market_snapshot", return_value=("无", "无")), \
             patch.object(G, "_safe_extra", side_effect=lambda fn, default, label: default), \
             patch.object(G, "_calendar_degraded", return_value=False):
            result = G.prepare_node({"as_of": NOW.isoformat()})
        ctx = result["ctx"]
        for row in rows:
            if row.title in ctx["all_news"] or row.title in ctx["macro_news"]:
                self.assertIn(row.content, ctx["news_fulltext"])
        self.assertEqual(result["data_freshness"]["evidence_excluded_count"], 2)
        self.assertIn("覆盖限制", ctx["evidence_selection_note"])
        self.assertEqual(ctx["evidence_issues"], [])


class LoggingContracts(unittest.TestCase):
    def setUp(self):
        self.messages = []
        sink = logger.add(lambda message: self.messages.append(str(message)), format="{message}")
        self.addCleanup(logger.remove, sink)

    def run_report(self, action, passed):
        db = MagicMock(info={"daily_report_write": {"action": action}})
        report = SimpleNamespace(id="report-id", date=NOW, sentiment="中性", score=0.3,
                                 content=json.dumps({"is_passed": passed, "critic_status": "incomplete_sources"}))
        with patch.object(T, "is_trading_day", return_value=True), \
             patch("app.db.SessionLocal", return_value=db), \
             patch.object(R, "run_daily_pipeline", return_value=report), \
             patch.object(S.settings, "report_wait_max_minutes", 0):
            S._job_report()
        db.close.assert_called_once()
        return "\n".join(self.messages)

    def test_discarded_attempt_is_not_logged_as_generated_success(self):
        text = self.run_report("kept_existing", True)
        self.assertIn("本轮新稿被丢弃", text)
        self.assertNotIn("每日研判完成", text)
        self.assertNotIn("新稿已保存", text)

    def test_saved_failed_draft_is_explicitly_unpassed(self):
        text = self.run_report("updated_failed", False)
        self.assertIn("新稿已保存但审计未通过", text)
        self.assertIn("incomplete_sources", text)

    def test_saved_passed_report_is_logged_as_new(self):
        self.assertIn("新稿已保存", self.run_report("updated", True))

    def test_market_job_logs_coverage_and_closes_session(self):
        db = MagicMock()
        with patch.object(T, "is_trading_day", return_value=True), \
             patch("app.db.SessionLocal", return_value=db), \
             patch.object(M, "collect_and_store_market", return_value=0), \
             patch.object(M, "check_market_coverage", return_value={"history_status": "incomplete"}):
            S._job_market()
        self.assertIn("历史覆盖=incomplete", "\n".join(self.messages))
        db.close.assert_called_once()

    def test_critic_failure_binds_degraded_reason(self):
        result = G.critic_node({"opinions": [{"expert": "宏观"}],
                                "ctx": {"evidence_issues": ["缺失"]}, "final": {}})["final"]
        self.assertTrue(result["degraded"])
        self.assertEqual(result["degraded_reason"], "critic_incomplete_sources")

    def test_existing_degradation_reason_is_preserved(self):
        result = G.critic_node({"opinions": [{"expert": "宏观"}], "ctx": {},
                                "final": {"degraded": True, "degraded_reason": "chief_summary_missing"}})["final"]
        self.assertEqual(result["degraded_reason"], "chief_summary_missing")

    def test_calendar_history_is_read_only_and_excludes_today(self):
        dates = {date(2026, 10, day) for day in (23, 26, 27, 28)}
        with patch.object(T, "_loaded", True), patch.object(T, "_degraded", False), \
             patch.object(T, "_dates", dates), patch.object(T, "_load", side_effect=AssertionError("禁止联网")):
            self.assertEqual(T.recent_trading_days(NOW.date(), 2), [date(2026, 10, 27), date(2026, 10, 26)])
            self.assertIsNone(T.recent_trading_days(NOW.date(), 7))

    def test_frontend_discloses_budget_exclusion_without_claiming_no_news(self):
        source = Path("frontend/streamlit_app.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("page_dashboard", "_fmt_score")]
        st = MagicMock()
        st.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
        namespace = {"st": st}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        namespace["page_dashboard"]({"low_info": True, "content": {
            "data_freshness": {"evidence_excluded_count": 3},
        }}, include_market=False)
        warnings = "\n".join(call.args[0] for call in st.warning.call_args_list)
        self.assertIn("3 条候选新闻", warnings)
        self.assertFalse(any("💤" in call.args[0] for call in st.info.call_args_list))

    def test_chief_handles_budget_exclusion_as_limited_coverage(self):
        prompts = []
        llm = RunnableLambda(lambda prompt: prompts.append(prompt) or AIMessage(content='{"market_summary":"覆盖受限"}'))
        quant = G.aggregate_node({"info_level": {"low_info": True},
                                  "data_freshness": {"evidence_excluded_count": 3}})["quant"]
        E.run_chief(llm, {"evidence_selection_note": "3 条新闻未纳入，须说明覆盖限制"}, [], RiskOpinion(), quant, part="expert")
        self.assertIn("覆盖限制", prompts[0])
        self.assertNotIn("请在 market_summary 中如实说明「今日无重大消息」", prompts[0])


if __name__ == "__main__":
    logger.remove()
    unittest.main()
