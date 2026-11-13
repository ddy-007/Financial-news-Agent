"""Read archived articles for an existing news group as of a local-time cutoff."""
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models.news_ingest import RawNews

_MAX_ITEMS = 50
_CORRECTION = re.compile(r"^(?:【(?:更正|勘误|纠错)】|(?:更正|勘误|纠错)(?:[:：\s]|公告|声明))")
_RESPONSE = re.compile(r"(?:【回应】|(?:回应|答复|澄清)(?:[:：]|公告|声明))")


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _link(value) -> str | None:
    url = _text(value)
    if not url or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            return url
    except ValueError:
        pass
    return None


def read_event_timeline(db: Session, news_id: str, cutoff: datetime) -> dict:
    # Both timestamps matter: a later update can carry the same publication date as an older article.
    rows = (db.query(RawNews)
            .filter(RawNews.news_id == news_id, RawNews.status == "processed",
                    RawNews.publish_time <= cutoff, RawNews.created_at <= cutoff)
            .order_by(RawNews.publish_time, RawNews.created_at, RawNews.id)
            .limit(_MAX_ITEMS + 1).all())
    items = []
    skipped = 0
    for row in rows[:_MAX_ITEMS]:
        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError):
            payload = None
        title = _text(payload.get("title")) if isinstance(payload, dict) else ""
        if not title:
            skipped += 1
            continue

        content = _text(payload.get("content"))
        kind = "correction" if _CORRECTION.search(title) else "response" if _RESPONSE.search(title) else "report"
        items.append({
            "id": row.id, "title": title, "source": row.source, "url": _link(payload.get("url")),
            "published_at": row.publish_time.isoformat(), "archived_at": row.created_at.isoformat(),
            "kind": kind, "preview": content[:1200], "preview_truncated": len(content) > 1200,
        })
    return {"cutoff": cutoff.isoformat(), "items": items, "truncated": len(rows) > _MAX_ITEMS, "skipped": skipped}
