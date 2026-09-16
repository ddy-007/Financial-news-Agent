"""新闻相关接口。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.news import News
from app.services.news_service import collect_and_store_news

router = APIRouter(prefix="/api/v1/news", tags=["news"])


def _news_dict(n: News) -> dict:
    return {
        "id": n.id,
        "title": n.title,
        "content": n.content,
        "source": n.source,
        "url": n.url,
        "publish_time": n.publish_time.isoformat() if n.publish_time else None,
        "category": n.category,
        "market": n.market,
        "themes": n.themes,
        "source_count": n.source_count,
        "sentiment": n.sentiment,
        "summary": n.summary,
    }


@router.get("")
def list_news(
    keyword: str | None = None,
    days: int | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(News).order_by(News.publish_time.desc())
    if keyword:
        q = q.filter(News.title.contains(keyword))
    if days:
        since = datetime.now() - timedelta(days=days)
        q = q.filter(News.publish_time >= since)
    rows = q.limit(limit).all()
    return [_news_dict(n) for n in rows]


@router.post("/collect")
def collect(db: Session = Depends(get_db)):
    """手动触发一次新闻采集 + 入库 + 索引。"""
    n = collect_and_store_news(db)
    return {"added": n}
