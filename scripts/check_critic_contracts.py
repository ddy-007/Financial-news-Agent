"""Critic 闭环契约：假模型、MockTransport、内存 SQLite，禁止真实模型和生产写入。

运行：.venv/Scripts/python.exe -X utf8 -B scripts/check_critic_contracts.py
"""
from __future__ import annotations

import ast
import json
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.agent.critic as C
import app.agent.experts as E
import app.agent.graph as G
import app.agent.llm as L
from app.agent.schemas import CriticVerdict, ExpertOpinion, RiskOpinion
from app.models.base import Base
from app.models.news import News
from app.models.news_ingest import RawNews

NOW = datetime(2026, 10, 23, 18, 30)
PASS = {"is_passed": True, "flaws": [], "revision_instructions": ""}
FAIL = {"is_passed": False, "flaws": ["market_summary：利润率 50% 无出处，原文为亏损"],
        "revision_instructions": "删除 50% 利润率，补充公司亏损和诉讼风险"}


class VerdictContracts(unittest.TestCase):
    def test_rejects_contradictions_coercion_and_empty_rejections(self):
        for data in (dict(PASS, flaws=["虚构数据"]), dict(PASS, revision_instructions="删除"),
                     dict(PASS, is_passed="true"), dict(FAIL, flaws=[]),
                     dict(FAIL, flaws=[" "]), dict(FAIL, revision_instructions=" ")):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                CriticVerdict.model_validate(data)

    def test_structured_tool_call_and_binding_survive_fallback(self):
        seen = []
        starts = []

        class Recorder(BaseCallbackHandler):
            def on_chat_model_start(self, serialized, messages, **kwargs):
                starts.append(kwargs)

        def transport(request):
            body = json.loads(request.content)
            seen.append(body)
            self.assertEqual(body["tools"][0]["function"]["name"], "CriticVerdict")
            # 结构校验在各级备用内部，主模型矛盾输出必须切备用。
            verdict = dict(PASS, flaws=["不能一边通过一边报错"]) if body["model"] == "primary" else PASS
            return httpx.Response(200, json={
                "id": "mock", "model": body["model"], "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "verdict", "type": "function", "function": {
                            "name": "CriticVerdict", "arguments": json.dumps(verdict),
                        },
                    }],
                }}],
            })

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            def model(name):
                return L._FallbackAwareChatOpenAI(
                    api_key="synthetic-test", base_url="https://mock.invalid/v1", model=name,
                    http_client=client, max_retries=0,
                )
            llm = model("primary").with_fallbacks(
                [model("backup")], exceptions_to_handle=L._FALLBACK_EXCEPTIONS,
            ).with_config(tags=["critic-contract"], callbacks=[Recorder()])
            result = C.run_critic(llm, {"news_fulltext": "原文全文，含亏损与诉讼风险"},
                                  {"expert_opinions": [{"evidence": ["待审底稿"]}]})
        self.assertTrue(result.is_passed)
        self.assertEqual([body["model"] for body in seen], ["primary", "backup"])
        self.assertEqual(len(starts), 2)
        self.assertTrue(all("critic-contract" in start["tags"] for start in starts))
        prompt = seen[-1]["messages"]
        self.assertEqual(prompt[0]["role"], "system")
        self.assertIn("原文全文，含亏损与诉讼风险", prompt[1]["content"])
        self.assertIn("待审底稿", prompt[1]["content"])

    def test_no_tool_call_is_not_a_pass(self):
        primary = MagicMock()
        primary.with_structured_output.return_value = RunnableLambda(lambda _: None)
        with self.assertRaises(ValidationError):
            C.run_critic(primary, {"news_fulltext": "原文"}, {})

    def test_all_six_roles_receive_fulltext_and_revision_feedback(self):
        prompts = []
        content = json.dumps({"score": 0, "stance": "中性", "market_summary": "综述"})
        llm = RunnableLambda(lambda prompt: prompts.append(prompt) or AIMessage(content=content))
        ctx = {"news_fulltext": "全文中的诉讼风险，正文末尾", "critic_feedback": "删除虚构利润率"}
        for fn in (E.run_macro_expert, E.run_industry_expert, E.run_capital_expert, E.run_technical_expert):
            fn(llm, ctx, part="expert")
        E.run_risk_officer(llm, ctx, part="expert")
        quant = G.aggregate_node({"opinions": []})["quant"]
        E.run_chief(llm, ctx, [], RiskOpinion(), quant, part="expert")
        self.assertEqual(len(prompts), 6)
        for prompt in prompts:
            self.assertIn(ctx["news_fulltext"], prompt)
            self.assertIn(ctx["critic_feedback"], prompt)


class GraphContracts(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.ctx = {"news_fulltext": "公司亏损 10 亿元，并涉及诉讼。", "evidence_issues": []}
        self.prepare = self.stack.enter_context(patch.object(G, "prepare_node", side_effect=lambda _: {
            "ctx": dict(self.ctx), "retry_count": 0, "critic_feedback": "", "is_passed": False,
            "critic_status": "pending",
        }))
        self.llm = self.stack.enter_context(patch.object(G, "get_llm", return_value=object()))
        self.stack.enter_context(patch.object(G, "get_llm_model_name", return_value="fake-model"))
        self.calls = {name: [] for name in G.EXPERT_WEIGHTS}
        self.fail_macro_first = False
        for name, fn in zip(G.EXPERT_WEIGHTS, (
            "run_macro_expert", "run_industry_expert", "run_capital_expert", "run_technical_expert",
        )):
            def expert(llm, ctx, *, part, name=name):
                self.calls[name].append(dict(ctx))
                if name == "宏观" and self.fail_macro_first and len(self.calls[name]) == 1:
                    raise ConnectionError("fake transient failure")
                return ExpertOpinion(expert=name, score=-0.4 if ctx.get("critic_feedback") else 0.4)
            self.stack.enter_context(patch.object(G, fn, side_effect=expert))
        self.risk = self.stack.enter_context(patch.object(G, "run_risk_officer", side_effect=lambda llm, ctx, **kw:
            RiskOpinion(risk_level="高" if ctx.get("critic_feedback") else "低", risks=["公司亏损和诉讼风险"])))
        self.chief = self.stack.enter_context(patch.object(G, "run_chief", side_effect=lambda *a, **kw: {
            "title": "公司风险提示", "market_summary": "公司亏损，存在诉讼风险", "risks": ["亏损和诉讼"],
        }))
        self.audit = self.stack.enter_context(patch.object(G, "run_critic", return_value=CriticVerdict(**PASS)))

    def invoke(self):
        return G.build_graph().invoke({})

    def test_pass_first_draft_ends_without_revision(self):
        result = self.invoke()
        self.assertTrue(result["is_passed"])
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(self.llm.call_count, 7)
        self.assertEqual(len(result["opinions"]), 4)

    def test_revision_recalculates_quant_and_clears_previous_reducers(self):
        self.fail_macro_first = True
        self.audit.side_effect = [CriticVerdict(**FAIL), CriticVerdict(**PASS)]
        result = self.invoke()
        self.assertTrue(result["is_passed"])
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(len(result["opinions"]), 4)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["retryable_failures"], [])
        self.assertEqual(result["final"]["failed_experts"], [])
        self.assertAlmostEqual(result["final"]["weighted_score"], -0.4)
        self.assertTrue(result["final"]["risk_veto"])
        self.prepare.assert_called_once()
        for calls in self.calls.values():
            self.assertEqual(len(calls), 2)
            self.assertIn(FAIL["revision_instructions"], calls[1]["critic_feedback"])
            self.assertEqual(calls[0]["news_fulltext"], calls[1]["news_fulltext"])

    def test_three_rejections_stop_at_two_revisions(self):
        self.audit.return_value = CriticVerdict(**FAIL)
        result = self.invoke()
        self.assertFalse(result["is_passed"])
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(self.audit.call_count, 3)
        self.assertEqual(self.llm.call_count, 21)
        self.assertEqual(result["final"]["critic_status"], "rejected")
        self.assertEqual(result["final"]["critic_flaws"], FAIL["flaws"])
        self.assertTrue(result["final"]["degraded"])
        self.assertEqual(len(result["opinions"]), 4)

    def test_new_invocation_does_not_reuse_previous_audit_state(self):
        self.audit.side_effect = [CriticVerdict(**FAIL), CriticVerdict(**PASS), CriticVerdict(**PASS)]
        graph = G.build_graph()
        first = graph.invoke({})
        second = graph.invoke({})
        self.assertEqual(first["retry_count"], 1)
        self.assertEqual(second["retry_count"], 0)
        self.assertEqual(second["critic_feedback"], "")
        self.assertEqual(len(second["opinions"]), 4)

    def test_model_failure_is_unpassed_and_does_not_waste_full_rounds(self):
        self.audit.side_effect = ValueError("invalid output")
        result = self.invoke()
        self.assertFalse(result["is_passed"])
        self.assertEqual(result["critic_status"], "error")
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["final"]["draft_model"], "fake-model")
        self.assertIsNone(result["final"]["critic_model"])
        self.assertEqual(self.audit.call_count, 1)

    def test_incomplete_fulltext_never_calls_audit_or_revises(self):
        self.ctx["evidence_issues"] = ["归档正文缺失"]
        result = self.invoke()
        self.assertFalse(result["is_passed"])
        self.assertEqual(result["critic_status"], "incomplete_sources")
        self.assertIsNone(result["final"]["critic_model"])
        self.audit.assert_not_called()

    def test_empty_chief_is_repaired_before_audit(self):
        self.chief.side_effect = None
        self.chief.return_value = {"market_summary": "", "title": "日报生成暂不可用"}
        result = self.invoke()
        audited = self.audit.call_args.args[2]
        self.assertIn("综合定调", audited["market_summary"])
        self.assertEqual(audited["market_summary"], result["final"]["market_summary"])

    def test_all_analysts_fail_skips_audit_and_keeps_failure_accounting(self):
        for fn in (G.run_macro_expert, G.run_industry_expert, G.run_capital_expert, G.run_technical_expert):
            fn.side_effect = ConnectionError("offline")
        result = self.invoke()
        self.assertEqual(result["critic_status"], "skipped")
        self.assertFalse(result["is_passed"])
        self.assertEqual(set(result["final"]["failed_experts"]), set(G.EXPERT_WEIGHTS))
        self.audit.assert_not_called()

    def test_audit_state_is_saved_with_report(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.audit.return_value = CriticVerdict(**FAIL)
        with Session(engine) as db, patch.object(G, "get_app", return_value=G.build_graph()), \
             patch.object(G, "_fallback_report", side_effect=AssertionError("不得重写审核后的报告")):
            report = G.generate_daily_report(db, NOW)
            content = json.loads(report.content)
        self.assertFalse(content["is_passed"])
        self.assertEqual(content["retry_count"], 2)
        self.assertIn(FAIL["revision_instructions"], content["critic_feedback"])
        self.assertAlmostEqual(report.score, -0.4)

    def test_report_model_is_not_replaced_by_critic_fallback_model(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        original_chief = self.chief.side_effect

        def record(name):
            L._ModelRecorder("expert", "draft-model").on_chat_model_start(
                {}, [], metadata={"ls_model_name": name},
            )

        def chief(*args, **kwargs):
            record("draft-model")
            return original_chief(*args, **kwargs)

        def critic(*args, **kwargs):
            record("critic-backup")
            return CriticVerdict(**PASS)

        self.chief.side_effect = chief
        self.audit.side_effect = critic
        with patch.dict(L._USED_MODEL, {}, clear=True), \
             patch.object(L, "note_primary_failed", return_value=None), \
             patch.object(G, "get_llm_model_name", side_effect=L.get_llm_model_name), \
             patch.object(G, "get_app", return_value=G.build_graph()), Session(engine) as db:
            report = G.generate_daily_report(db, NOW)
            content = json.loads(report.content)
        self.assertEqual(report.model, "draft-model")
        self.assertEqual(content["draft_model"], "draft-model")
        self.assertEqual(content["critic_model"], "critic-backup")

    def test_each_revision_keeps_its_own_draft_and_critic_models(self):
        current_model = ["initial"]
        self.stack.enter_context(patch.object(G, "get_llm_model_name", side_effect=lambda **kw: current_model[0]))
        original_chief = self.chief.side_effect
        rounds = []

        def chief(*args, **kwargs):
            rounds.append(len(rounds) + 1)
            current_model[0] = f"draft-{len(rounds)}"
            return original_chief(*args, **kwargs)

        def critic(*args, **kwargs):
            current_model[0] = f"critic-{len(rounds)}"
            return CriticVerdict(**(FAIL if len(rounds) == 1 else PASS))

        self.chief.side_effect = chief
        self.audit.side_effect = critic
        final = self.invoke()["final"]
        self.assertEqual(final["retry_count"], 1)
        self.assertEqual(final["draft_model"], "draft-2")
        self.assertEqual(final["critic_model"], "critic-2")

    def test_missing_chief_does_not_claim_critic_model_authored_report(self):
        self.chief.side_effect = ConnectionError("chief unavailable")
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with patch.object(G, "get_app", return_value=G.build_graph()), Session(engine) as db:
            report = G.generate_daily_report(db, NOW)
            final = json.loads(report.content)
            self.assertIsNone(report.model)
            self.assertIsNone(final["draft_model"])
            self.assertEqual(final["critic_model"], "fake-model")

    def test_fallback_cannot_inherit_previous_pass(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(G, "_run_experts", return_value=({
            "market_summary": "", "is_passed": True, "critic_status": "passed",
            "critic_model": "previous-critic",
        }, False)), patch.object(G, "_fallback_report", return_value={"market_summary": "兜底正文"}):
            content = json.loads(G.generate_daily_report(db, NOW).content)
        self.assertFalse(content["is_passed"])
        self.assertEqual(content["critic_status"], "not_reviewed")
        self.assertIsNone(content["critic_model"])

    def test_summary_patch_invalidates_previous_audit_model(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db, patch.object(G, "_run_experts", return_value=({
            "market_summary": "", "expert_opinions": [{"expert": "宏观"}],
            "is_passed": True, "critic_model": "previous-critic", "draft_model": "old-writer",
        }, False)), patch.object(G, "_fallback_report") as fallback:
            report = G.generate_daily_report(db, NOW)
            content = json.loads(report.content)
            self.assertIsNone(report.model)
        fallback.assert_not_called()
        self.assertFalse(content["is_passed"])
        self.assertIsNone(content["critic_model"])

    def test_single_model_fallback_records_its_own_draft_model(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.llm.return_value = RunnableLambda(lambda _: AIMessage(content=json.dumps({
            "title": "单次兜底", "market_summary": "单次生成的报告", "score": 0.1,
        })))
        with Session(engine) as db, patch.object(G, "_market_snapshot", return_value=("无", "无")), \
             patch.object(G, "_calendar_degraded", return_value=False), \
             patch.object(G, "get_llm_model_name", return_value="single-writer"):
            final = G._fallback_report(db, NOW)
        self.assertEqual(final["draft_model"], "single-writer")


class EvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.news = News(id="news", title="原始标题", content="公司亏损 10 亿元，存在诉讼风险", source="测试",
                         publish_time=NOW, dedup_policy=C.PAIRWISE_POLICY)
        self.db.add(self.news)
        self.db.commit()

    def add_raw(self, key="raw", *, body="公司亏损 10 亿元，存在诉讼风险", published=NOW, source="测试", url=None):
        self.db.add(RawNews(id=key, news_id="news", source=source, publish_time=published,
                           payload=json.dumps({"title": "原始标题", "content": body,
                                               "source": source, "url": url,
                                               "publish_time": published.isoformat()})))
        self.db.commit()

    def test_fulltext_and_all_merged_originals_are_read_without_truncation(self):
        body = "新闻正文" * 1000 + "末尾存在法律风险"
        self.news.content = body
        self.add_raw(body=body)
        self.add_raw("raw2", body="另一原文有负债 5 亿元")
        evidence, issues = C.load_news_evidence(self.db, [self.news, self.news], NOW)
        self.assertEqual(issues, [])
        self.assertIn(body, evidence)
        self.assertIn("另一原文有负债 5 亿元", evidence)
        self.assertNotIn("合并摘要不能充当原文", evidence)
        self.assertEqual(len(evidence.splitlines()), 2)

    def test_legacy_partial_archive_cannot_prove_complete_lineage(self):
        self.news.dedup_policy = None
        self.add_raw()
        _, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertTrue(any("部分补档" in issue for issue in issues))

    def test_missing_sources_or_representative_are_not_accepted(self):
        self.news.source_urls = json.dumps([{"source": "另一来源", "url": "https://mock.invalid/article"}])
        self.add_raw()
        _, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertTrue(any("缺少对应归档" in issue for issue in issues))
        self.news.source_urls = None
        self.news.content = "尚未归档的新版本"
        self.db.commit()
        _, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertTrue(any("缺少对应归档" in issue for issue in issues))

    def test_merged_longer_article_can_keep_older_publish_time(self):
        older = NOW - timedelta(hours=1)
        self.news.publish_time = older
        self.news.content = "更完整的新成员正文，新增诉讼风险"
        self.add_raw("old", published=older)
        self.add_raw("new", body=self.news.content, published=NOW)
        evidence, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertEqual(issues, [])
        self.assertIn("新增诉讼风险", evidence)

    def test_display_url_can_belong_to_another_archived_source(self):
        self.news.source = "无链接代表源"
        self.news.url = "https://mock.invalid/article"
        real = {"source": "实际链接源", "url": self.news.url}
        self.news.source_urls = json.dumps([real])
        self.add_raw("representative", source=self.news.source)
        self.add_raw("linked", source=real["source"], url=real["url"], body="另一源的完整正文")
        for mapping in ([real], [real, {"source": self.news.source, "url": self.news.url}]):
            self.news.source_urls = json.dumps(mapping)
            self.db.commit()
            _, issues = C.load_news_evidence(self.db, [self.news], NOW)
            self.assertEqual(issues, [])

    def test_missing_or_empty_fulltext_is_explicit(self):
        _, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertIn("缺少归档全文", issues[0])
        self.add_raw(body="")
        _, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertTrue(issues)

    def test_future_source_is_not_in_historical_evidence(self):
        self.add_raw(published=NOW + timedelta(seconds=1))
        evidence, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertNotIn("公司亏损", evidence)
        self.assertTrue(issues)

    def test_budget_and_member_limit_fail_closed(self):
        self.add_raw()
        with patch.object(C, "MAX_EVIDENCE_CHARS", 10):
            evidence, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertEqual(evidence, "无可核验原文")
        self.assertIn("上下文预算", issues[0])
        self.add_raw("raw2")
        with patch.object(C, "MAX_RAW_PER_NEWS", 1):
            _, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertIn("超过 1 条", issues[0])

    def test_malformed_archive_is_not_replaced_by_summary(self):
        self.db.add(RawNews(id="bad", news_id="news", source="测试", payload="[]"))
        self.db.commit()
        evidence, issues = C.load_news_evidence(self.db, [self.news], NOW)
        self.assertTrue(issues)
        self.assertNotIn("合并摘要", evidence)


class FrontendContracts(unittest.TestCase):
    def test_rejected_report_exposes_status_and_feedback(self):
        source = Path("frontend/streamlit_app.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ("page_dashboard", "_fmt_score")]
        st = MagicMock()
        st.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
        namespace = {"st": st}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        namespace["page_dashboard"]({"content": {
            "is_passed": False, "retry_count": 2, "critic_status": "rejected",
            "critic_feedback": "删除无依据利润率",
        }}, include_market=False)
        self.assertIn("审计未通过", st.error.call_args.args[0])
        self.assertIn("已修正 2 次", st.error.call_args.args[0])
        self.assertTrue(any(call.args == ("删除无依据利润率",) for call in st.write.call_args_list))


if __name__ == "__main__":
    logger.remove()
    unittest.main()
