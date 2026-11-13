"""Exercise the real read-only timeline route using temporary SQLite."""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes_news import router
from app.db import get_db
from app.models.news_ingest import RawNews


class EventTimelineContracts(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        RawNews.__table__.create(self.engine)
        app = FastAPI()
        app.include_router(router)

        def test_db():
            with Session(self.engine) as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        self.client = TestClient(app)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.client.close)

    def add(self, id, day, archived_day, title, *, group="event", status="processed", url=None, content="原文"):
        with Session(self.engine) as db:
            db.add(RawNews(
                id=id, news_id=group, status=status, source="原始媒体",
                publish_time=datetime(2026, 10, day, 9) if day else None,
                created_at=datetime(2026, 10, archived_day, 10),
                payload=json.dumps({"title": title, "content": content, "url": url}),
            ))
            db.commit()

    def test_cutoff_filters_both_original_publication_and_late_archival(self):
        url = "https://example.com/news/1"
        self.add("report", 1, 1, "事件首次报道", url=url)
        self.add("response", 2, 2, "公司回应：说明", url="https://example.com/news/2")
        self.add("correction", 3, 3, "【更正】事件数据", url="javascript:alert(1)")
        self.add("later-version", 1, 4, "同 URL 后来更新的版本", url=url)
        self.add("future-publication", 5, 1, "发布时间在截点之后")
        self.add("unknown-time", None, 1, "发布时间未知")
        self.add("other", 1, 1, "其他事件", group="elsewhere")
        self.add("pending", 1, 1, "还未处理", status="pending")

        response = self.client.get("/api/v1/news/event/timeline", params={"as_of": "2026-10-03"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["cutoff"], "2026-10-03T23:59:59.999999")
        self.assertEqual([item["id"] for item in result["items"]], ["report", "response", "correction"])
        self.assertEqual([item["kind"] for item in result["items"]], ["report", "response", "correction"])
        self.assertEqual(result["items"][0]["url"], url)
        self.assertIsNone(result["items"][2]["url"])
        latest = self.client.get("/api/v1/news/event/timeline").json()["items"]
        self.assertEqual([item["id"] for item in latest[:2]], ["report", "later-version"])

    def test_output_is_bounded_and_empty_is_explicit(self):
        for index in range(51):
            self.add(f"{index:02}", 1, 1, "报道", content="原" * 1300)
        result = self.client.get("/api/v1/news/event/timeline").json()
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["items"]), 50)
        self.assertEqual(len(result["items"][0]["preview"]), 1200)
        self.assertTrue(result["items"][0]["preview_truncated"])
        self.assertEqual(self.client.get("/api/v1/news/missing/timeline").json()["items"], [])
        self.assertEqual(self.client.get("/api/v1/news/event/timeline?as_of=bad").status_code, 422)


if __name__ == "__main__":
    unittest.main()
