"""日报故障回归：假模型、内存 SQLite、内存日志，不调用真实服务。

运行：.venv/Scripts/python.exe -X utf8 -B scripts/check_daily_report_contracts.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.agent.graph as G
import app.services.report_service as R
from app.models.base import Base
from app.models.report import MarketReport

NOW = datetime(2026, 10, 23, 18, 30)


class DailyReportContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.stack.enter_context(patch.object(G, "get_llm", side_effect=AssertionError("禁止真实模型调用")))
        self.stack.enter_context(patch.object(G, "get_llm_model_name", return_value="mock-model"))
        self.stack.enter_context(patch.object(G, "_calendar_degraded", return_value=False))
        self.stack.enter_context(patch.object(G.settings, "report_retry_when_partial", 0))
        self.fallback = {
            "title": "单次生成", "market_summary": "独立看多结论", "sentiment": "偏多",
            "score": 0.6, "confidence": "low", "risk_veto": False,
            "risks": ["兜底风险"], "expert_opinions": [],
            "data_freshness": {"stale": None, "note": "降级模式，未做数据时效检查"},
        }

    def generate(self, final, *, error=None, fallback=None):
        with patch.object(G, "_run_experts", return_value=(final, False)), \
             patch.object(G, "_fallback_report", return_value=fallback or self.fallback,
                          side_effect=error) as call:
            report = G.generate_daily_report(self.db, NOW)
        return report, json.loads(report.content), call

    def no_analysts(self, *, risks=None, level="高"):
        return {
            "failed_experts": list(G.EXPERT_WEIGHTS), "expert_opinions": [],
            "market_summary": "", "risk_veto": level == "高",
            "risk_opinion": {"risk_level": level, "risks": risks or []},
        }

    def test_missing_chief_uses_existing_results_without_llm(self):
        for summary in ("", " \n", "（首席汇总失败）"):
            with self.subTest(summary=summary):
                opinions = [{"expert": "宏观", "score": -0.5, "stance": "看空"}]
                final = {
                    "title": "日报生成暂不可用", "market_summary": summary,
                    "expert_opinions": opinions, "weighted_score": -0.5,
                    "sentiment": "偏空", "final_confidence": "low", "divergence": 0.8,
                    "risk_veto": True, "risk_opinion": {"risk_level": "高", "risks": ["流动性风险"]},
                }
                report, content, call = self.generate(final)
                call.assert_not_called()
                self.assertIn("偏空", content["market_summary"])
                self.assertIn("-0.500", content["market_summary"])
                self.assertIn("流动性风险", content["market_summary"])
                self.assertNotIn("独立看多", content["market_summary"])
                self.assertEqual(content["expert_opinions"], opinions)
                self.assertEqual(report.score, -0.5)
                self.assertTrue(report.risk_veto)
                self.assertEqual(report.divergence, 0.8)
                self.assertNotEqual(report.title, "日报生成暂不可用")

    def test_successful_chief_is_preserved(self):
        _, content, call = self.generate({"title": "原首席标题", "market_summary": "原首席综述"})
        self.assertEqual(content["market_summary"], "原首席综述")
        self.assertNotIn("degraded_reason", content)
        call.assert_not_called()

    def test_successful_fallback_merges_every_risk(self):
        report, content, _ = self.generate(self.no_analysts(risks=["流动性风险", "兜底风险"]))
        self.assertEqual(content["risks"], ["兜底风险", "流动性风险"])
        self.assertEqual(content["risk_opinion"]["risks"], ["流动性风险", "兜底风险"])
        self.assertTrue(report.risk_veto)

    def test_high_risk_with_empty_list_still_preserves_veto(self):
        report, _, _ = self.generate(self.no_analysts())
        self.assertTrue(report.risk_veto)

    def test_medium_risk_does_not_create_veto(self):
        report, content, _ = self.generate(self.no_analysts(level="中", risks=["中风险"]))
        self.assertFalse(report.risk_veto)
        self.assertIn("中风险", content["risks"])

    def test_string_fallback_risk_is_not_split_into_characters(self):
        fallback = dict(self.fallback, risks="模型风险")
        _, content, _ = self.generate(self.no_analysts(risks=["已知风险"]), fallback=fallback)
        self.assertEqual(content["risks"], ["模型风险", "已知风险"])

    def test_failed_fallback_normalizes_risk_without_character_splitting(self):
        _, content, _ = self.generate(
            self.no_analysts(risks="已知风险"), error=ConnectionError("mock offline"),
        )
        self.assertEqual(content["risks"], ["已知风险"])
        self.assertTrue(content["risk_veto"])

    def test_unknown_freshness_survives_both_fallback_outcomes(self):
        for error in (None, ConnectionError("mock offline")):
            with self.subTest(error=error):
                # Use a fresh day row so prior successful fallback cannot mask the failure case.
                self.db.query(MarketReport).delete()
                self.db.commit()
                _, content, _ = self.generate({"_graph_error": "ConnectionError"}, error=error)
                self.assertIsNone(content["data_freshness"]["stale"])
                self.assertIn("未做", content["data_freshness"]["note"])

    def test_existing_freshness_is_preserved(self):
        freshness = {"stale": True, "note": "原时效说明", "open_gap_count": 2}
        final = dict(self.no_analysts(), data_freshness=freshness)
        report, content, _ = self.generate(final)
        self.assertEqual(content["data_freshness"], freshness)
        self.assertTrue(report.data_stale)

    def test_failed_rerun_keeps_successful_report_unchanged(self):
        first, _, _ = self.generate({"title": "已有好报告", "market_summary": "完整研判", "score": 0.6})
        original = (first.id, first.date, first.title, first.content, first.score)
        second, _, _ = self.generate(self.no_analysts(), error=ConnectionError("mock offline"))
        self.assertEqual((second.id, second.date, second.title, second.content, second.score), original)
        self.assertEqual(self.db.query(MarketReport).count(), 1)

    def test_first_failure_is_saved_and_success_can_replace_it(self):
        first, content, _ = self.generate(self.no_analysts(), error=ConnectionError("mock offline"))
        self.assertTrue(content["env"]["generation_failed"])
        second, content, _ = self.generate({"title": "恢复成功", "market_summary": "完整研判", "score": 0.4})
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.title, "恢复成功")
        self.assertEqual(second.score, 0.4)
        self.assertNotIn("generation_failed", content.get("env", {}))

    def test_audit_failure_cannot_replace_an_existing_passed_report(self):
        first, _, _ = self.generate({
            "title": "已审计通过", "market_summary": "有依据的原报告", "score": 0.6,
            "is_passed": True, "critic_status": "passed",
        })
        original = (first.id, first.date, first.title, first.content, first.score)
        for status in ("rejected", "error", "incomplete_sources", "not_reviewed", "skipped"):
            with self.subTest(status=status):
                kept, _, _ = self.generate({
                    "title": "审计失败新稿", "market_summary": "未核验的新正文", "score": -0.7,
                    "is_passed": False, "critic_status": status, "degraded": True,
                })
                self.assertEqual((kept.id, kept.date, kept.title, kept.content, kept.score), original)
        self.assertEqual(self.db.query(MarketReport).count(), 1)

    def test_first_audit_failure_is_saved_and_passed_result_can_replace_it(self):
        first, failed, _ = self.generate({
            "market_summary": "首份审计失败记录", "is_passed": False, "critic_status": "rejected",
        })
        self.assertFalse(failed["is_passed"])
        original_id = first.id
        recovered, passed, _ = self.generate({
            "market_summary": "更正后审计通过", "is_passed": True, "critic_status": "passed", "score": 0.4,
        })
        self.assertEqual(recovered.id, original_id)
        self.assertTrue(passed["is_passed"])
        self.assertEqual(recovered.score, 0.4)

    def test_passed_audit_can_update_an_existing_passed_report(self):
        first, _, _ = self.generate({"market_summary": "通过的第一版", "is_passed": True})
        original_id = first.id
        second, _, _ = self.generate({"market_summary": "通过的第二版", "is_passed": True})
        self.assertEqual(second.id, original_id)
        self.assertEqual(json.loads(second.content)["market_summary"], "通过的第二版")

    def test_concurrent_audit_failure_does_not_replace_winner(self):
        winner = SimpleNamespace(id=42, date=NOW, content=json.dumps({"is_passed": True}), score=0.6)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, winner]
        db.commit.side_effect = IntegrityError("mock insert", {}, Exception("unique conflict"))
        result = R.upsert_daily_report(
            db, NOW.date(), score=-0.7,
            content=json.dumps({"is_passed": False, "critic_status": "error"}),
        )
        self.assertIs(result, winner)
        self.assertEqual(winner.score, 0.6)
        self.assertTrue(json.loads(winner.content)["is_passed"])
        db.rollback.assert_called_once()
        db.refresh.assert_called_once_with(winner)
        self.assertEqual(db.commit.call_count, 1)

    def test_successful_rerun_still_updates_same_row(self):
        first, _, _ = self.generate({"market_summary": "第一版", "score": 0.2})
        second, _, _ = self.generate({"market_summary": "第二版", "score": 0.7})
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.score, 0.7)

    def test_stale_failed_row_cannot_overwrite_other_sessions_success(self):
        first, _, _ = self.generate(self.no_analysts(), error=ConnectionError("mock offline"))
        self.assertTrue(R._generation_failed(first.content))
        with Session(self.engine) as other:
            R.upsert_daily_report(
                other, NOW.date(), date=NOW, title="其他会话已恢复",
                content=json.dumps({"market_summary": "并发恢复的成功研判"}), score=0.7,
            )
        # The original session still caches the earlier failed content.
        self.assertTrue(R._generation_failed(first.content))
        kept, content, _ = self.generate(self.no_analysts(), error=ConnectionError("mock offline"))
        self.assertEqual(kept.score, 0.7)
        self.assertEqual(content["market_summary"], "并发恢复的成功研判")
        self.assertEqual(kept.title, "其他会话已恢复")

    def test_concurrent_failure_does_not_replace_winner(self):
        winner = SimpleNamespace(id=42, date=NOW, content=json.dumps({"market_summary": "并发成功"}), score=0.6)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, winner]
        db.commit.side_effect = IntegrityError("mock insert", {}, Exception("unique conflict"))
        result = R.upsert_daily_report(
            db, NOW.date(), score=0.0,
            content=json.dumps({"env": {"generation_failed": True}}),
        )
        self.assertIs(result, winner)
        self.assertEqual(winner.score, 0.6)
        db.rollback.assert_called_once()
        self.assertEqual(db.commit.call_count, 1)

    def test_failure_flag_is_compatible_with_legacy_content(self):
        for content in (None, "old text", "[]", "null", '{"env": "old"}'):
            self.assertFalse(R._generation_failed(content))
        self.assertTrue(R._generation_failed('{"degraded_reason":"fallback_failed"}'))
        self.assertTrue(R._generation_failed('{"is_passed":false}'))
        for content in ('{"is_passed":true}', '{"is_passed":null}', '{"is_passed":0}', '{"is_passed":"false"}'):
            self.assertFalse(R._generation_failed(content))


class LoggingContracts(unittest.TestCase):
    def test_console_and_file_do_not_expand_keys_and_setup_is_idempotent(self):
        # A child process retains Loguru's real default console sink. AST extraction
        # avoids importing app.main; both the file sink and model transport are mocked.
        code = r'''
import ast, io, json, sys
from pathlib import Path
from unittest.mock import patch
from loguru import logger
import app.agent.llm as L
file_sink = io.StringIO()
extra_sink = io.StringIO()
logger.add(extra_sink, diagnose=False)
class FakePath:
    def __init__(self, *args): pass
    def mkdir(self, **kwargs): pass
namespace = {"logger": logger, "sys": sys, "Path": FakePath,
             "_LOG_SINK_ADDED": False, "_bridge_stdlib_logging": lambda: None}
tree = ast.parse(Path("app/main.py").read_text(encoding="utf-8"))
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_setup_file_logging")
exec(compile(ast.Module(body=[node], type_ignores=[]), "app/main.py", "exec"), namespace)
original_add = logger.add
file_adds = []
def memory_add(sink, **kwargs):
    if isinstance(sink, str):
        file_adds.append(sink)
        kwargs.pop("rotation", None)
        kwargs.pop("retention", None)
        kwargs.pop("encoding", None)
        kwargs["enqueue"] = False
        sink = file_sink
    return original_add(sink, **kwargs)
with patch.object(logger, "add", side_effect=memory_add):
    namespace["_setup_file_logging"]()
    namespace["_setup_file_logging"]()
try:
    with patch.object(L, "_FallbackAwareChatOpenAI", side_effect=ValueError("mock construction failure")):
        L._make_client("SYNTHETIC_AUDIT_KEY_7A9B", "https://mock.invalid", "mock", 0.0)
except Exception:
    logger.exception("synthetic exception")
print(json.dumps({"file_key": "SYNTHETIC_AUDIT_KEY_7A9B" in file_sink.getvalue(),
                  "file_trace": "_make_client" in file_sink.getvalue(),
                  "extra_preserved": "synthetic exception" in extra_sink.getvalue(),
                  "file_adds": len(file_adds)}))
'''
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, encoding="utf-8", timeout=45,
        )
        self.assertEqual(result.returncode, 0, "日志隔离子进程失败")
        self.assertNotIn("SYNTHETIC_AUDIT_KEY_7A9B", result.stderr)
        self.assertIn("_make_client", result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["file_key"])
        self.assertTrue(data["file_trace"])
        self.assertTrue(data["extra_preserved"])
        self.assertEqual(data["file_adds"], 1)


if __name__ == "__main__":
    logger.remove()
    unittest.main()
