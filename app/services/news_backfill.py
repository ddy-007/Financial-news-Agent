"""有界区间补采：先做只读预览，再显式应用本地预览；不操作增量水位线。"""
from __future__ import annotations

import json
import math
from datetime import datetime

from sqlalchemy.orm import Session

from app.agent.llm import effective_fallback_models
from app.collectors.news_collector import SourceResult
from app.models.news import News
from app.models.news_ingest import CollectionGap, RawNews
from app.services.news_archive import (
    acknowledge, archive_items, decode_item, item_key, item_payload,
    record_gap,
)


def known_urls(db: Session) -> dict[str, str]:
    """包含已合并来源链接，不能只检查 News.url。"""
    out = {}
    for row in db.query(News).all():
        if row.url:
            out[row.url] = row.id
        for pair in json.loads(row.source_urls or "[]"):
            if isinstance(pair, dict) and pair.get("url"):
                out[pair["url"]] = row.id
    return out


def build_preview(db: Session, result: SourceResult, start: datetime, end: datetime,
                  *, raw_table_available: bool = True) -> dict:
    if start >= end:
        raise ValueError("补采区间必须满足 start < end")
    items = {item_key(it): it for it in result.items
             if it.publish_time and start <= it.publish_time < end}
    urls = known_urls(db)
    missing = []
    for key, it in items.items():
        row = db.get(RawNews, key) if raw_table_available else None
        if it.url not in urls and (row is None or row.status != "processed"):
            missing.append(key)
    newest = max((it.publish_time for it in result.items if it.publish_time), default=None)
    covered = bool(result.ok and result.reached_since and not result.truncated
                   and not result.rejected and newest and newest >= end)
    attempts = len(effective_fallback_models("news")) + 1
    if attempts == 1:
        attempts = 3  # 无备用时外层最多三次尝试
    return {
        "version": 1, "source": result.source, "start": start.isoformat(), "end": end.isoformat(),
        "created_at": datetime.now().isoformat(), "window_covered": covered,
        "stop_reason": result.stop_reason, "failed_pages": result.failed_pages,
        "rejected_count": len(result.rejected), "observed_count": len(items),
        "missing_count": len(missing),
        # 每个候选最多一次灰区请求，另加批量分类；费用依实际模型单价与 token 数计算。
        "model_request_upper_bound": attempts * (len(missing) + math.ceil(len(missing) / 20)),
        "items": [item_payload(it) for it in items.values()],
    }


def apply_preview(db: Session, preview: dict, *, max_items: int) -> dict:
    """调用方必须持有采集锁；显式授权后才执行，本函数可能调用付费模型。"""
    from app.agent.data_agent import run_data_agent

    if preview.get("version") != 1:
        raise ValueError("不支持的补采预览版本")
    start, end = datetime.fromisoformat(preview["start"]), datetime.fromisoformat(preview["end"])
    if start >= end or start.tzinfo is not None or end.tzinfo is not None:
        raise ValueError("补采必须使用有效的本地无时区时间区间")
    items = {item_key(it): it for it in map(decode_item, preview["items"])}
    for it in items.values():
        if (it.source != preview["source"] or not it.publish_time or not it.title.strip()
                or not start <= it.publish_time < end):
            raise ValueError("预览含区间或来源不符的条目")
    urls = known_urls(db)
    todo, known = [], {}
    for key, it in items.items():
        row = db.get(RawNews, key)
        if row is not None and row.status == "processed":
            continue
        if it.url in urls and not (row and row.status == "index_pending"):
            known[key] = urls[it.url]
        else:
            todo.append(it)
    if max_items < 0 or len(todo) > max_items:
        raise ValueError(f"待处理 {len(todo)} 条，超过显式上限 {max_items}；未入库或调用模型")
    archive_items(db, list(items.values()))
    record_gap(db, preview["source"], start, end)
    db.commit()
    acknowledge(db, known)
    result = run_data_agent(db, raw=todo)
    db.expire_all()
    pending = sum(db.get(RawNews, key).status != "processed" for key in items)
    resolved = 0
    # 必须有上下界覆盖证据，且全部处理完成；普通成功轮永远不会调用此关闭逻辑。
    if preview.get("window_covered") is True and not pending:
        for gap in db.query(CollectionGap).filter(
                CollectionGap.source == preview["source"], CollectionGap.status != "resolved",
                CollectionGap.start_at >= start, CollectionGap.end_at <= end).all():
            gap.status = "resolved"
            gap.resolved_at = datetime.now()
            resolved += 1
        db.commit()
    return {**result, "pending": pending, "resolved_gaps": resolved,
            "window_covered": preview.get("window_covered") is True}
