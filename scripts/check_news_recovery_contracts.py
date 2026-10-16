"""问题 4/5 离线契约：完整链接、原文恢复、抓取进度、缺口及定向补采。

只使用假采集器/假模型/假向量与内存或临时 SQLite；不碰业务库，不联网。
"""
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
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import app.agent.data_agent as DA
import app.db as DB
import app.services.news_service as NS
from app.collectors.news_collector import (
    Anchor, ClsCollector, EastmoneyCollector, NewsItem, SinaCollector, SourceResult,
)
from app.models.base import Base
from app.models.collector_state import CollectorState
from app.models.news import News
from app.models.news_ingest import CollectionGap, RawNews
from app.services.collector_health import evaluate
from app.services.news_archive import (
    acknowledge, archive_items, coverage_counts, decode_item, item_key, pending_items,
    record_gap, save_capture,
)
from app.services.news_backfill import apply_preview, build_preview


NOW = datetime(2026, 10, 16, 18, 30)


def item(name="A", *, source="新浪财经", url=None, minutes=0, content=None):
    return NewsItem(title=name, content=content or name, source=source, url=url,
                    publish_time=NOW + timedelta(minutes=minutes))


def result(items, **kwargs):
    return SourceResult(source="新浪财经", items=items, rejected=[], ok=True, **kwargs)


def classified(items):
    return [{"item": it, "relevant": True, "category": "综合", "market": "无", "themes": []}
            for it in items], []


@contextmanager
def fake_pipeline(*, classifier=classified, vectors=None, index=None):
    with ExitStack() as stack:
        stack.enter_context(patch.object(DA, "classify_news", side_effect=classifier))
        stack.enter_context(patch.object(DA, "_embed_candidates", side_effect=lambda c:
            np.eye(len(c), dtype=np.float32) if vectors is None else vectors[:len(c)]))
        stack.enter_context(patch.object(DA, "_nearest_existing", side_effect=lambda db, c, v: [None] * len(c)))
        stack.enter_context(patch.object(DA, "_upsert_index", side_effect=index or (lambda rows: None)))
        stack.enter_context(patch.object(DA, "_rebuild_bm25", return_value=None))
        yield


class RecoveryContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_complete_link_breaks_chain_above_gray_limit(self):
        angles = np.radians([0, 20, 40])
        vecs = np.stack([np.cos(angles), np.sin(angles)], axis=1)
        groups = DA._cluster_by_similarity(vecs)
        self.assertEqual(len(groups), 2)
        self.assertFalse(any(0 in g and 2 in g for g in groups))
        self.assertEqual(sorted(sum(groups, [])), [0, 1, 2])

    def test_complete_link_all_pairs_property_and_chunking(self):
        rng = np.random.default_rng(45)
        vecs = rng.normal(size=(18, 5))
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        for chunk in (1, 7, 256):
            groups = DA._cluster_by_similarity(vecs, threshold=0.7, chunk=chunk)
            self.assertEqual(sorted(sum(groups, [])), list(range(len(vecs))))
            for g in groups:
                self.assertLessEqual(len(g), DA.MAX_CLUSTER_SIZE)
                self.assertTrue(np.all(vecs[g] @ vecs[g].T >= 0.7 - 1e-10))

    def test_exact_duplicates_still_merge_and_oversize_still_splits(self):
        vecs = np.ones((3, 1))
        self.assertEqual(DA._cluster_by_similarity(vecs), [[0, 1, 2]])
        self.assertEqual(len(DA._cluster_by_similarity(np.ones((21, 1)))), 21)

    def test_raw_identity_preserves_full_versions_and_is_idempotent(self):
        a, b = item(url="https://example/a"), item(url="https://example/a", content="修订正文")
        archive_items(self.db, [a, a, b])
        self.db.commit()
        archive_items(self.db, [a, b])
        self.db.commit()
        self.assertEqual(self.db.query(RawNews).count(), 2)
        self.assertEqual({it.content for it in pending_items(self.db)}, {a.content, b.content})
        self.assertEqual(decode_item(self.db.get(RawNews, item_key(a)).payload), a)

    def test_capture_is_independent_of_classification_and_keeps_gap(self):
        r = result([item(minutes=0), item("B", minutes=-30)], truncated=True,
                   requested_since=NOW - timedelta(hours=2))
        save_capture(self.db, [r], {})
        NS.save_states(self.db, [r], classify_failed=2)
        st = self.db.get(CollectorState, "新浪财经")
        self.assertIsNone(st.last_ts)
        self.assertIsNone(st.last_ok_at)
        self.assertEqual(st.fetched_ts, NOW)
        self.assertEqual(NS.load_states(self.db)["新浪财经"].last_ts, NOW)
        self.assertEqual(coverage_counts(self.db), {"pending_count": 2, "open_gap_count": 1})
        good = result([item("new", minutes=5)], requested_since=NOW - timedelta(minutes=15),
                      reached_since=True)
        save_capture(self.db, [good], NS.load_states(self.db))
        NS.save_states(self.db, [good], 0)
        self.assertEqual(self.db.query(CollectionGap).one().status, "open")
        self.assertIn("gap", evaluate(self.db, now=NOW)["sources"][0]["flags"])

    def test_source_failure_does_not_advance_capture(self):
        r = result([item()], requested_since=NOW - timedelta(hours=1))
        r.ok, r.failed_pages = False, 1
        save_capture(self.db, [r], {})
        self.assertIsNone(self.db.get(CollectorState, "新浪财经"))
        self.assertEqual(self.db.query(RawNews).count(), 1)

    def test_empty_page_after_new_items_keeps_unproven_window_as_gap(self):
        r = result([item()], requested_since=NOW - timedelta(hours=2),
                   reached_since=False, stop_reason="exhausted")
        save_capture(self.db, [r], {})
        gap = self.db.query(CollectionGap).one()
        self.assertEqual((gap.start_at, gap.end_at), (NOW - timedelta(hours=2), NOW))

    def test_capture_rollback_never_leaves_watermark_without_raw(self):
        with patch("app.services.news_archive.record_gap", side_effect=RuntimeError("simulate transaction failure")):
            with self.assertRaises(RuntimeError):
                save_capture(self.db, [result([item()], truncated=True)], {})
        self.db.rollback()
        self.assertEqual(self.db.query(RawNews).count(), 0)
        self.assertEqual(self.db.query(CollectorState).count(), 0)

    def test_all_cluster_originals_have_lineage(self):
        items = [item("A", url="https://example/a"), item("B", source="财联社", url="https://example/b")]
        with fake_pipeline(vectors=np.ones((2, 1))):
            res = DA.run_data_agent(self.db, raw=items)
        self.assertEqual(res["new"], 1)
        originals = self.db.query(RawNews).all()
        self.assertEqual(len(originals), 2)
        self.assertEqual({r.status for r in originals}, {"processed"})
        self.assertEqual(len({r.news_id for r in originals}), 1)
        self.assertTrue(all(r.news_id for r in originals))

    def test_partial_classification_only_retries_failed_originals(self):
        a, b = item("A"), item("B", source="财联社")
        def partial(items):
            return classified(items[:1])[0], items[1:]
        with fake_pipeline(classifier=partial):
            DA.run_data_agent(self.db, raw=[a, b])
        self.assertEqual(pending_items(self.db), [b])
        with fake_pipeline():
            DA.run_data_agent(self.db, raw=pending_items(self.db))
        self.assertEqual(pending_items(self.db), [])
        self.assertEqual(self.db.query(News).count(), 2)

    def test_failed_cluster_retains_every_member_for_retry(self):
        items = [item("A"), item("B")]
        with fake_pipeline(classifier=lambda xs: ([], xs), vectors=np.ones((2, 1))):
            DA.run_data_agent(self.db, raw=items)
        self.assertEqual({it.title for it in pending_items(self.db)}, {"A", "B"})

    def test_irrelevant_originals_are_retained_without_news(self):
        a = item()
        def irrelevant(xs):
            rows, _ = classified(xs)
            for row in rows:
                row["relevant"] = False
            return rows, []
        with fake_pipeline(classifier=irrelevant):
            DA.run_data_agent(self.db, raw=[a])
        row = self.db.query(RawNews).one()
        self.assertEqual(row.status, "processed")
        self.assertIsNone(row.news_id)
        self.assertEqual(self.db.query(News).count(), 0)

    def test_index_failure_recovers_without_classifying_again(self):
        a = item(url="https://example/a")
        with fake_pipeline(index=lambda rows: (_ for _ in ()).throw(RuntimeError("index down"))):
            with self.assertRaises(RuntimeError):
                DA.run_data_agent(self.db, raw=[a])
        self.assertEqual(self.db.query(RawNews).one().status, "index_pending")
        self.assertEqual(self.db.query(News).count(), 1)
        with fake_pipeline(classifier=lambda xs: self.fail("recovery must not classify")):
            DA.run_data_agent(self.db, raw=pending_items(self.db))
        self.assertEqual(pending_items(self.db), [])
        self.assertEqual(self.db.query(News).count(), 1)

    def test_same_url_different_versions_all_survive(self):
        a = item(url="https://example/a")
        b = item("revision", url=a.url, content="修订")
        with fake_pipeline():
            DA.run_data_agent(self.db, raw=[a, b])
        self.assertEqual(self.db.query(RawNews).count(), 2)
        self.assertEqual(coverage_counts(self.db)["pending_count"], 0)
        self.assertEqual(len({r.news_id for r in self.db.query(RawNews)}), 1)

    def test_reprocessing_processed_no_url_original_is_free_and_idempotent(self):
        a = item()
        with fake_pipeline():
            DA.run_data_agent(self.db, raw=[a])
        with fake_pipeline(classifier=lambda xs: self.fail("processed item must not classify")):
            DA.run_data_agent(self.db, raw=[a])
        self.assertEqual(self.db.query(News).count(), 1)

    def preview(self, *, covered=True):
        a = item("novel", url="https://example/new", minutes=-30)
        r = result([item("before", minutes=-121), a, item("after", minutes=1)],
                   reached_since=covered, stop_reason="reached_since" if covered else "page_limit")
        return build_preview(self.db, r, NOW - timedelta(hours=2), NOW)

    def test_preview_is_readonly_and_checks_merged_urls(self):
        self.db.add(News(title="canonical", source="财联社", url="https://example/main",
                         source_urls=json.dumps([{"source": "新浪财经", "url": "https://example/new"}])))
        self.db.commit()
        with patch("app.services.news_backfill.effective_fallback_models", return_value=[]):
            preview = self.preview()
        self.assertEqual(preview["missing_count"], 0)
        self.assertEqual(preview["model_request_upper_bound"], 0)
        self.assertEqual(self.db.query(RawNews).count(), 0)
        self.assertTrue(preview["window_covered"])

    def test_empty_or_truncated_scan_never_proves_full_coverage(self):
        for r in [result([], stop_reason="exhausted"),
                  result([item(minutes=-180), item(minutes=1)], reached_since=True, truncated=True)]:
            self.assertFalse(build_preview(self.db, r, NOW - timedelta(hours=2), NOW)["window_covered"])

    def test_apply_item_limit_precedes_any_model_or_write(self):
        preview = self.preview()
        with self.assertRaises(ValueError), patch.object(DA, "run_data_agent") as agent:
            apply_preview(self.db, preview, max_items=0)
        agent.assert_not_called()
        self.assertEqual(self.db.query(RawNews).count(), 0)

    def test_apply_resolves_only_matching_fully_covered_gaps(self):
        start = NOW - timedelta(hours=2)
        target = record_gap(self.db, "新浪财经", start, NOW)
        other = record_gap(self.db, "财联社", start, NOW)
        larger = record_gap(self.db, "新浪财经", start - timedelta(hours=1), NOW)
        self.db.commit()
        with fake_pipeline():
            outcome = apply_preview(self.db, self.preview(), max_items=1)
        self.assertEqual(outcome["resolved_gaps"], 1)
        self.assertEqual(target.status, "resolved")
        self.assertEqual((other.status, larger.status), ("open", "open"))
        self.assertEqual(self.db.query(CollectorState).count(), 0)

    def test_partial_scan_or_classification_failure_keeps_gap_open(self):
        gap = record_gap(self.db, "新浪财经", NOW - timedelta(hours=2), NOW)
        self.db.commit()
        with fake_pipeline():
            apply_preview(self.db, self.preview(covered=False), max_items=1)
        self.assertEqual(gap.status, "open")
        preview = self.preview()
        preview["items"] = [json.dumps({**json.loads(preview["items"][0]),
                                       "title": "different", "url": "https://example/other"})]
        with fake_pipeline(classifier=lambda xs: ([], xs)):
            out = apply_preview(self.db, preview, max_items=1)
        self.assertEqual(out["pending"], 1)
        self.assertEqual(gap.status, "open")

    def test_apply_validates_item_source_and_interval(self):
        p = self.preview()
        p["source"] = "财联社"
        with self.assertRaises(ValueError):
            apply_preview(self.db, p, max_items=1)
        self.assertEqual(self.db.query(RawNews).count(), 0)

    def test_gap_health_survives_next_clean_round(self):
        st = CollectorState(source="新浪财经", last_ok_at=NOW, empty_streak=0)
        self.db.add(st)
        record_gap(self.db, "新浪财经", NOW - timedelta(hours=2), NOW)
        self.db.commit()
        report = evaluate(self.db, results=[result([item()])], now=NOW)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["execution_status"], "success")
        self.assertEqual(report["sources"][0]["status"], "gap")

    def test_pending_means_processing_incomplete_even_when_fetch_succeeds(self):
        self.db.add(CollectorState(source="新浪财经", last_ok_at=NOW, empty_streak=0))
        archive_items(self.db, [item()])
        self.db.commit()
        report = evaluate(self.db, results=[result([item()])], now=NOW)
        h = report["sources"][0]
        self.assertEqual(h["fetch_status"], "success")
        self.assertEqual(h["execution_status"], "partial_failure")
        self.assertEqual(h["failure_stage"], "processing")

    def test_pending_limit_does_not_discard_tail(self):
        archive_items(self.db, [item(str(i)) for i in range(4)])
        self.db.commit()
        self.assertEqual(len(pending_items(self.db, limit=2)), 2)
        self.assertEqual(coverage_counts(self.db)["pending_count"], 4)

    def test_new_collection_reads_pending_archive_even_without_new_items(self):
        a = item()
        archive_items(self.db, [a])
        self.db.commit()
        with patch.object(NS, "run_collection", return_value=([], [])), fake_pipeline():
            self.assertEqual(NS.collect_and_store_news(self.db), 1)
        self.assertEqual(pending_items(self.db), [])

    def test_collector_preview_bounds_do_not_mutate_global_settings(self):
        pages = [
            {"result": {"data": {"feed": {"list": [
                {"rich_text": "new", "create_time": "2026-10-16 18:30:00"}]}}}},
            {"result": {"data": {"feed": {"list": [
                {"rich_text": "old", "create_time": "2026-10-16 15:30:00"}]}}}},
        ]
        collector = SinaCollector()
        with patch.object(collector, "_get_json", side_effect=pages) as get:
            res = collector.fetch(since=NOW - timedelta(hours=2), max_pages=2)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(res.reached_since)
        self.assertFalse(res.truncated)
        with patch.object(collector, "_get_json", return_value=pages[0]):
            res = collector.fetch(since=NOW - timedelta(hours=2), max_pages=1)
        self.assertTrue(res.truncated)
        self.assertFalse(res.reached_since)
        for collector in (SinaCollector(), EastmoneyCollector(), ClsCollector()):
            with patch.object(collector, "_get_json") as get:
                res = collector.fetch(max_pages=2, max_seconds=0)
            get.assert_not_called()
            self.assertEqual(res.stop_reason, "time_limit")

    def test_process_lock_excludes_a_second_handle_and_releases(self):
        with NS.collection_guard() as acquired:
            self.assertTrue(acquired)
            second = NS._try_process_lock()
            if second is not None:
                second.close()
            self.assertIsNone(second)
        with NS.collection_guard() as acquired:
            self.assertTrue(acquired)

    def test_migration_is_additive_and_idempotent_on_old_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "old.db"))
            try:
                with engine.begin() as conn:
                    conn.execute(text("CREATE TABLE collector_state (source VARCHAR PRIMARY KEY, last_ts DATETIME, last_id VARCHAR, last_ok_at DATETIME, empty_streak INTEGER, truncated_at DATETIME, updated_at DATETIME)"))
                    conn.execute(text("INSERT INTO collector_state (source,last_ts) VALUES ('新浪财经','2026-10-15 13:59:56')"))
                DB.init_news_ingest_schema(engine)
                DB.init_news_ingest_schema(engine)
                self.assertTrue({"raw_news", "collection_gap"} <= set(inspect(engine).get_table_names()))
                with Session(engine) as db:
                    st = db.get(CollectorState, "新浪财经")
                    self.assertEqual(st.last_ts, datetime(2026, 10, 15, 13, 59, 56))
                    self.assertIsNone(st.fetched_ts)
            finally:
                engine.dispose()


if __name__ == "__main__":
    from loguru import logger
    logger.remove()
    unittest.main(verbosity=2)
