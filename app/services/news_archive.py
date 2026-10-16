"""先保存原文，再处理；水位和原文同一事务提交，处理失败可跨进程重试。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collectors.news_collector import NewsItem, SourceResult, _cutoff
from app.models.collector_state import CollectorState
from app.models.news_ingest import CollectionGap, RawNews

PROCESS_LIMIT = 2000


def item_payload(item: NewsItem) -> str:
    data = asdict(item)
    data["publish_time"] = item.publish_time.isoformat() if item.publish_time else None
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def item_key(item: NewsItem) -> str:
    return hashlib.sha256(item_payload(item).encode("utf-8")).hexdigest()


def decode_item(payload: str) -> NewsItem:
    data = json.loads(payload)
    data["publish_time"] = datetime.fromisoformat(data["publish_time"]) if data.get("publish_time") else None
    return NewsItem(**data)


def archive_items(db: Session, items: list[NewsItem]) -> None:
    """仅加入当前事务；调用方决定与哪些进度一起提交。"""
    unique = {item_key(it): it for it in items}
    existing = set()
    keys = list(unique)
    for start in range(0, len(keys), 500):
        existing.update(k for k, in db.query(RawNews.id).filter(
            RawNews.id.in_(keys[start:start + 500])).all())
    for key, item in unique.items():
        if key not in existing:
            db.add(RawNews(id=key, source=item.source, publish_time=item.publish_time,
                           payload=item_payload(item), status="pending"))
    db.flush()


def record_gap(db: Session, source: str, start: datetime, end: datetime) -> CollectionGap | None:
    if start >= end:
        return None
    key = hashlib.sha256(f"{source}|{start.isoformat()}|{end.isoformat()}".encode()).hexdigest()
    gap = db.get(CollectionGap, key)
    if gap is None:
        gap = CollectionGap(id=key, source=source, start_at=start, end_at=end, status="open")
        db.add(gap)
        db.flush()
    return gap


def save_capture(db: Session, results: list[SourceResult], anchors: dict) -> None:
    """原文、抓取进度及截断缺口原子落库；源失败时原文保留、抓取进度不推进。"""
    now = datetime.now()
    archive_items(db, [it for r in results for it in r.items])
    for r in results:
        times = [it.publish_time for it in r.items if it.publish_time]
        if not r.ok or not times:
            continue
        st = db.get(CollectorState, r.source)
        if st is None:
            st = CollectorState(source=r.source, empty_streak=0)
            db.add(st)
        if r.truncated or not r.reached_since:
            record_gap(db, r.source, r.requested_since or _cutoff(anchors.get(r.source)), min(times))
        latest = max(times)
        if st.fetched_ts is None or latest > st.fetched_ts:
            st.fetched_ts = latest
        st.fetched_at = now
    db.commit()


def pending_items(db: Session, limit: int = PROCESS_LIMIT) -> list[NewsItem]:
    rows = db.query(RawNews).filter(RawNews.status != "processed").order_by(
        RawNews.created_at, RawNews.id).limit(limit).all()
    return [decode_item(r.payload) for r in rows]


def acknowledge(db: Session, outcomes: dict[str, str | None]) -> None:
    """在业务入库和索引全部完成后确认；None 表示已判断为无关。"""
    keys = list(outcomes)
    for start in range(0, len(keys), 500):
        for row in db.query(RawNews).filter(RawNews.id.in_(keys[start:start + 500])).all():
            row.status = "processed"
            row.news_id = outcomes[row.id]
            row.processed_at = datetime.now()
    db.commit()


def coverage_counts(db: Session, *, since: datetime | None = None,
                    until: datetime | None = None) -> dict:
    pending = db.query(func.count(RawNews.id)).filter(RawNews.status != "processed")
    gaps = db.query(func.count(CollectionGap.id)).filter(CollectionGap.status != "resolved")
    if since is not None:
        pending = pending.filter(RawNews.publish_time >= since)
        gaps = gaps.filter(CollectionGap.end_at > since)
    if until is not None:
        pending = pending.filter(RawNews.publish_time <= until)
        gaps = gaps.filter(CollectionGap.start_at <= until)
    return {"pending_count": pending.scalar(), "open_gap_count": gaps.scalar()}
