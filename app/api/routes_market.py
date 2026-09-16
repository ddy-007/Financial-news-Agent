"""行情相关接口。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.market import MarketData
from app.services.market_service import collect_and_store_market

router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("")
def list_market(
    symbol: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(MarketData).order_by(MarketData.date.desc())
    if symbol:
        q = q.filter(MarketData.symbol == symbol)
    rows = q.limit(limit).all()
    return [
        {
            "symbol": r.symbol,
            "name": r.name,
            "date": r.date.isoformat(),
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
            "change_pct": r.change_pct,
            "turnover": r.turnover,
        }
        for r in rows
    ]


@router.post("/collect")
def collect(db: Session = Depends(get_db)):
    """手动触发一次行情采集 + 入库。"""
    n = collect_and_store_market(db)
    return {"added": n}
