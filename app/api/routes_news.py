"""新闻相关接口。"""
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
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


@router.get("/dates")
def news_dates(db: Session = Depends(get_db)):
    """库中有新闻的日期清单（含每天条数）。

    **前端靠它生成「按日期筛选」的标签和覆盖率提示，所以必须完整。**
    原先前端是拿一批新闻（`limit=1000`）再从中"推"出日期 —— 数据量一超过
    这个上限，老日期连按钮都不会出现（实证：库里 4632 条覆盖 10 天，
    但前端那 1000 条只剩 09-19/09-20 两天）。

    本接口是一条聚合查询，返回十几行，**与新闻总量无关**。
    """
    day = func.substr(News.publish_time, 1, 10)
    rows = (
        db.query(day.label("d"), func.count().label("n"))
        .group_by("d")
        .order_by(day.desc())
        .all()
    )
    return {"dates": [{"date": r.d, "count": r.n} for r in rows]}


@router.get("")
def list_news(
    keyword: str | None = None,
    days: int | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """新闻列表。

    - `days=N`：近 N 天
    - `start`/`end`：按**发布日期**区间（含两端），用于"只看某一天"
    - 三者都不传：行为与本接口引入这些参数前一致（取最新 `limit` 条）
    """
    q = db.query(News).order_by(News.publish_time.desc())
    if keyword:
        q = q.filter(News.title.contains(keyword))
    if days:
        since = datetime.now() - timedelta(days=days)
        q = q.filter(News.publish_time >= since)
    if start:
        q = q.filter(News.publish_time >= datetime.combine(start, time.min))
    if end:
        q = q.filter(News.publish_time <= datetime.combine(end, time.max))
    rows = q.limit(limit).all()
    return [_news_dict(n) for n in rows]


@router.post("/collect")
def collect(db: Session = Depends(get_db)):
    """手动触发一次新闻采集 + 入库 + 索引。"""
    n = collect_and_store_news(db)
    return {"added": n}
