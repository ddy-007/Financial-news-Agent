"""有限分数与跨轮完整成员准入回归；假模型/向量、内存及临时SQLite，不联网不写业务库。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import app.agent.data_agent as DA
import app.agent.experts as E
import app.db as DB
from app.agent.schemas import ExpertOpinion
from app.collectors.news_collector import NewsItem, SourceResult
from app.models.base import Base
from app.models.news import News
from app.models.news_ingest import RawNews
from app.services.news_archive import archive_items, item_key
from app.services.news_backfill import build_preview
from app.services.semantic_merge import PAIRWISE_POLICY, SemanticMergeGuard, member_text

NOW = datetime(2026, 10, 19, 12)


def vec(degree):
    angle = np.radians(degree)
    return np.asarray([np.cos(angle), np.sin(angle)], dtype=np.float32)


def item(name, *, content=None, minutes=0, url=None):
    return NewsItem(title=name, content=content or name, source="test", url=url,
                    publish_time=NOW + timedelta(minutes=minutes))


class SemanticGuardContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.vectors = {}

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def register(self, it, degree):
        self.vectors[member_text(it)] = vec(degree)
        return it

    def embed(self, texts):
        return [self.vectors[t] for t in texts]

    def guard(self, cache=True):
        return SemanticMergeGuard(self.db, self.embed, self.vectors if cache else {})

    def seed(self, members, *, verified=True):
        first = members[0]
        row = News(title=first.title, content=first.content, source=first.source, url=first.url,
                   publish_time=first.publish_time, source_urls="[]",
                   dedup_policy=PAIRWISE_POLICY if verified else None)
        self.db.add(row)
        self.db.flush()
        archive_items(self.db, members)
        for it in members:
            raw = self.db.get(RawNews, item_key(it))
            raw.news_id = row.id
            raw.status = "processed"
        self.db.commit()
        return row

    @contextmanager
    def pipeline(self, target, *, classifier=None):
        def classify(items):
            return [{"item": it, "relevant": True, "category": "综合", "market": "无", "themes": []}
                    for it in items], []

        class Collection:
            def count(inner): return 1
            def query(inner, query_embeddings, n_results):
                anchor = self.vectors[f"{target.title}\n{target.content}"]
                return {"ids": [[target.id] for _ in query_embeddings],
                        "distances": [[1 - float(np.asarray(v) @ anchor)] for v in query_embeddings]}

        with ExitStack() as stack:
            stack.enter_context(patch.object(DA, "embed_documents", side_effect=self.embed))
            stack.enter_context(patch.object(DA.vector_store, "get_collection", return_value=Collection()))
            stack.enter_context(patch.object(DA, "classify_news", side_effect=classifier or classify))
            stack.enter_context(patch.object(DA, "_upsert_index"))
            stack.enter_context(patch.object(DA, "_rebuild_bm25"))
            yield

    def test_scores_reject_nonfinite_before_clamping(self):
        for bad in (float("nan"), float("inf"), -float("inf"), "nan", "NaN", "Infinity",
                    "-Infinity", "1e309", True, {"score": 0.3}, [0.3], "bad"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                E._clamp(bad)
        for value, want in ((None, 0), (0, 0), ("0.3", 0.3), (-1.2, -1), (1.2, 1)):
            self.assertEqual(E._clamp(value), want)

    def test_schema_also_rejects_nonfinite(self):
        for value in (float("nan"), float("inf"), -float("inf"), "nan"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ExpertOpinion(score=value)

    def test_invalid_score_triggers_backup(self):
        for value in ("nan", "Infinity", "1e309", True, {"value": 0.2}):
            calls = []
            def bad(prompt):
                calls.append("primary")
                return AIMessage(content=json.dumps({"score": value, "confidence": "high"}))
            def good(prompt):
                calls.append("backup")
                return AIMessage(content='{"score":0.2,"stance":"看多"}')
            model = RunnableLambda(bad).with_fallbacks([RunnableLambda(good)], exceptions_to_handle=(ValueError,))
            with self.subTest(value=value), patch("app.agent.llm.llm_retry_times", return_value=1):
                opinion = E.run_macro_expert(model, {}, part="expert")
                self.assertEqual(calls, ["primary", "backup"])
                self.assertEqual(opinion.score, 0.2)

    def test_all_scores_invalid_fail_expert_instead_of_returning_one(self):
        model = RunnableLambda(lambda prompt: AIMessage(content='{"score":"nan"}'))
        with patch("app.agent.llm.llm_retry_times", return_value=1), self.assertRaises(ValueError):
            E.run_macro_expert(model, {}, part="expert")

    def test_same_target_immediately_includes_accepted_member(self):
        b = self.register(item("B", content="BBBB"), 25)
        a, c = self.register(item("A"), 0), self.register(item("C"), 50)
        row = self.seed([b])
        guard = self.guard()
        self.assertTrue(guard.admit(row, [a]))
        self.assertFalse(guard.admit(row, [c]))

    def test_full_pipeline_cannot_remerge_split_endpoints_through_existing_row(self):
        b = self.register(item("B", content="BBBB"), 25)
        a, c = self.register(item("A"), 0), self.register(item("C"), 50)
        row = self.seed([b])
        with self.pipeline(row):
            result = DA.run_data_agent(self.db, raw=[a, c])
        self.assertEqual((result["within_round_merged"], result["merged"], result["new"]), (0, 1, 1))
        self.assertNotEqual(self.db.get(RawNews, item_key(a)).news_id,
                            self.db.get(RawNews, item_key(c)).news_id)

    def test_cross_round_restores_full_persisted_members(self):
        b = self.register(item("B", content="BBBB"), 25)
        a, c = self.register(item("A"), 0), self.register(item("C"), 50)
        row = self.seed([b])
        with self.pipeline(row):
            first = DA.run_data_agent(self.db, raw=[a])
        self.assertEqual(first["merged"], 1)
        key = row.id
        self.db.close()
        self.db = Session(self.engine)
        row = self.db.get(News, key)
        with self.pipeline(row):
            second = DA.run_data_agent(self.db, raw=[c])
        self.assertEqual((second["merged"], second["new"]), (0, 1))

    def test_legacy_row_is_not_certified_from_partial_available_raw(self):
        b, a = self.register(item("B"), 0), self.register(item("A"), 0)
        row = self.seed([b], verified=False)
        self.assertFalse(self.guard().admit(row, [a]))
        self.assertIsNone(row.dedup_policy)

    def test_missing_or_corrupt_lineage_freezes_semantic_admission(self):
        b = self.register(item("B"), 0)
        row = self.seed([b])
        raw = self.db.get(RawNews, item_key(b))
        for payload in ("bad JSON", "[]", "null", '"text"'):
            with self.subTest(payload=payload):
                raw.payload = payload
                row.dedup_policy = PAIRWISE_POLICY
                self.db.commit()
                self.assertFalse(self.guard().admit(row, [b]))
                self.assertIsNone(row.dedup_policy)
        self.db.delete(raw)
        row.dedup_policy = PAIRWISE_POLICY
        self.db.commit()
        self.assertFalse(self.guard().admit(row, [b]))

    def test_candidate_internal_pair_and_unknown_time_rejected(self):
        b = self.register(item("B"), 25)
        a, c = self.register(item("A"), 0), self.register(item("C"), 50)
        row = self.seed([b])
        self.assertFalse(self.guard().admit(row, [a, c]))
        a.publish_time = None
        self.assertFalse(self.guard().admit(row, [a]))

    def test_old_member_inconsistency_and_time_window_freeze_target(self):
        b, a = self.register(item("B"), 25), self.register(item("A"), 0)
        c = self.register(item("C"), 50)
        row = self.seed([b, a, c])
        self.assertFalse(self.guard().admit(row, [b]))
        self.assertIsNone(row.dedup_policy)
        self.assertFalse(self.guard().consistent([b, item("B", minutes=24 * 60 + 1)]))

    def test_membership_limit_checked_before_embedding_large_history(self):
        members = [self.register(item(str(i)), 0) for i in range(21)]
        row = self.seed(members)
        with patch.object(self, "embed", side_effect=AssertionError("must not embed oversized lineage")):
            self.assertFalse(self.guard(cache=False).admit(row, [members[0]]))

    def test_embedding_failure_invalid_vectors_and_dimension_mismatch_refuse_merge(self):
        b, a = self.register(item("B"), 0), self.register(item("A"), 0)
        row = self.seed([b])
        for encode in (lambda texts: (_ for _ in ()).throw(RuntimeError("offline")),
                       lambda texts: [[float("nan"), 1]] * len(texts),
                       lambda texts: [[0, 0]] * len(texts), lambda texts: []):
            row.dedup_policy = PAIRWISE_POLICY
            guard = SemanticMergeGuard(self.db, encode, {})
            self.assertFalse(guard.admit(row, [a]))

    def test_exact_url_update_keeps_identity_but_invalidates_inconsistent_cluster(self):
        b = self.register(item("B", url="https://example/b"), 0)
        changed = self.register(item("new B", content="changed long content", url=b.url), 90)
        row = self.seed([b])
        key = row.id
        with self.pipeline(row):
            result = DA.run_data_agent(self.db, raw=[changed])
        self.assertEqual((result["merged"], result["new"]), (1, 0))
        self.assertEqual(row.id, key)
        self.assertEqual(row.content, changed.content)
        self.assertIsNone(row.dedup_policy)

    def test_gray_candidate_never_spends_extra_review_to_bypass_constraint(self):
        b, a = self.register(item("B"), 0), self.register(item("A"), 40)
        row = self.seed([b])
        with self.pipeline(row), patch.object(DA, "_llm_is_duplicate", side_effect=AssertionError("gray bypass")):
            result = DA.run_data_agent(self.db, raw=[a])
        self.assertEqual((result["merged"], result["new"]), (0, 1))

    def test_transaction_rollback_does_not_persist_member_admission(self):
        b, a = self.register(item("B", content="BBBB"), 25), self.register(item("A"), 0)
        c = self.register(item("C"), 50)
        row = self.seed([b])
        original_commit = self.db.commit
        count = 0
        def commit():
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("before business transaction commit")
            return original_commit()
        with self.pipeline(row), patch.object(self.db, "commit", side_effect=commit), self.assertRaises(RuntimeError):
            DA.run_data_agent(self.db, raw=[a])
        self.db.rollback()
        self.assertIsNone(self.db.get(RawNews, item_key(a)).news_id)
        self.assertTrue(self.guard().admit(row, [c]))

    def test_index_failure_preserves_verified_members_for_retry(self):
        b, a = self.register(item("B", content="BBBB"), 25), self.register(item("A"), 0)
        row = self.seed([b])
        with self.pipeline(row), patch.object(DA, "_upsert_index", side_effect=RuntimeError("index down")), self.assertRaises(RuntimeError):
            DA.run_data_agent(self.db, raw=[a])
        raw = self.db.get(RawNews, item_key(a))
        self.assertEqual((raw.status, raw.news_id), ("index_pending", row.id))
        c = self.register(item("C"), 50)
        self.assertFalse(self.guard().admit(row, [c]))

    def test_prefix_fingerprint_cannot_certify_inconsistent_full_bodies(self):
        prefix = "p" * DA.FINGERPRINT_CONTENT_CHARS
        a = self.register(item("same", content=prefix + "A"), 0)
        c = self.register(item("same", content=prefix + "C"), 90)
        row = self.seed([self.register(item("old"), 180)], verified=False)
        with self.pipeline(row):
            result = DA.run_data_agent(self.db, raw=[a, c])
        self.assertEqual(result["new"], 2)
        self.assertNotEqual(self.db.get(RawNews, item_key(a)).news_id,
                            self.db.get(RawNews, item_key(c)).news_id)

    def test_admission_deterministic_when_candidate_order_changes(self):
        b = self.register(item("B", content="BBBB"), 25)
        a, c = self.register(item("A"), 0), self.register(item("C"), 50)
        row = self.seed([b])
        outcomes = []
        for candidates in ([a, c], [c, a]):
            guard = self.guard()
            outcomes.append([it.title for it in sorted(candidates, key=item_key) if guard.admit(row, [it])])
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(len(outcomes[0]), 1)

    def test_migration_preserves_old_data_and_adds_null_policy_and_index(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "old.db"))
            try:
                with engine.begin() as conn:
                    conn.execute(text("CREATE TABLE news (id VARCHAR PRIMARY KEY, title TEXT)"))
                    conn.execute(text("INSERT INTO news VALUES ('old','keep')"))
                    conn.execute(text("CREATE TABLE raw_news (id VARCHAR PRIMARY KEY, source VARCHAR, publish_time DATETIME, payload TEXT, status VARCHAR, news_id VARCHAR, created_at DATETIME, processed_at DATETIME)"))
                DB.init_news_ingest_schema(engine)
                DB.init_news_ingest_schema(engine)
                with engine.connect() as conn:
                    self.assertEqual(conn.execute(text("SELECT id,title,dedup_policy FROM news")).all(), [("old", "keep", None)])
                self.assertIn("ix_raw_news_news_id", {r["name"] for r in inspect(engine).get_indexes("raw_news")})
            finally:
                engine.dispose()

    def test_readonly_preview_handles_unmigrated_news_schema(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE news (id VARCHAR PRIMARY KEY, url VARCHAR, source_urls TEXT)"))
                conn.execute(text("INSERT INTO news VALUES ('old','https://example/known','[]')"))
            with Session(engine) as db:
                it = item("known", url="https://example/known")
                result = SourceResult(source="test", items=[it], rejected=[], ok=True)
                with patch("app.services.news_backfill.effective_fallback_models", return_value=[]):
                    preview = build_preview(db, result, NOW - timedelta(hours=1),
                                            NOW + timedelta(hours=1), raw_table_available=False)
                self.assertEqual(preview["missing_count"], 0)
                self.assertNotIn("raw_news", inspect(engine).get_table_names())
                self.assertNotIn("dedup_policy", {c["name"] for c in inspect(engine).get_columns("news")})
        finally:
            engine.dispose()


if __name__ == "__main__":
    from loguru import logger
    logger.remove()
    unittest.main(verbosity=2)
