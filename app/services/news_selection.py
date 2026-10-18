"""日报窗口内分时配额与段内重要性排序；只使用现有新闻，不调用模型。"""
import json
from datetime import datetime

from loguru import logger

from app.config import settings


def distinct_source_count(news) -> int:
    sources = {news.source} if news.source else set()
    try:
        pairs = json.loads(news.source_urls or "[]")
    except (TypeError, json.JSONDecodeError):
        logger.warning("[新闻选取] 来源链接格式异常，仅使用主来源计数")
        pairs = []
    if isinstance(pairs, list):
        sources.update(p["source"] for p in pairs if isinstance(p, dict)
                       and isinstance(p.get("source"), str) and p["source"])
    return max(1, len(sources))  # 不信任旧 source_count，不推断各媒体是否独立信源


def select_news(rows: list, limit: int, since: datetime, until: datetime) -> list:
    """等宽时段各有配额，余量由全窗口排序补足；同分按时间、ID 稳定排序。"""
    if limit <= 0 or since > until:
        return []
    rows = [r for r in rows if r.publish_time and since <= r.publish_time <= until]
    if not rows:
        return []
    segments = min(settings.news_selection_segments, limit)
    span = max((until - since).total_seconds(), 1)

    def rank(row):
        age = max(0.0, (until - row.publish_time).total_seconds() / 3600)
        score = ((1 + settings.news_source_weight * (distinct_source_count(row) - 1))
                 / (age + 2) ** settings.news_recency_gravity)
        return (-score, -row.publish_time.timestamp(), str(row.id or ""))

    ordered = sorted(rows, key=rank)
    buckets = [[] for _ in range(segments)]
    for row in ordered:
        index = min(segments - 1, int((row.publish_time - since).total_seconds() / span * segments))
        buckets[index].append(row)
    selected = []
    for index, bucket in enumerate(buckets):
        quota = limit // segments + (index < limit % segments)
        selected.extend(bucket[:quota])
    chosen = {id(row) for row in selected}
    selected.extend(row for row in ordered if id(row) not in chosen)
    return selected[:limit]
