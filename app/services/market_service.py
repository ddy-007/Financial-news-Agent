"""行情业务：采集 + 去重入库。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.collectors.market_collector import collect_all_market_data
from app.models.market import MarketData


def save_market_data(db: Session, rows: list[dict]) -> int:
    saved = 0
    for r in rows:
        exists = (
            db.query(MarketData)
            .filter(MarketData.symbol == r["symbol"], MarketData.date == r["date"])
            .first()
        )
        if exists:
            continue
        db.add(MarketData(**r))
        saved += 1
    db.commit()
    return saved


def collect_and_store_market(db: Session) -> int:
    """采集 + 入库（供调度器调用），返回新增条数。"""
    rows = collect_all_market_data()
    return save_market_data(db, rows)
