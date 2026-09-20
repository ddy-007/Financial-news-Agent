"""板块数据接口。"""
from datetime import date, datetime, time

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.sector_service import get_latest_sectors

router = APIRouter(prefix="/api/v1/sectors", tags=["sectors"])


@router.get("")
def list_sectors(target_date: date | None = None, db: Session = Depends(get_db)):
    """板块涨跌排行。

    - `target_date` 留空 → 取库中**最新一天**
    - 指定日期 → 取该日（含）之前最近的一天

    ⚠️ **返回的 `date` 必须展示给用户**：板块数据可能不是最新的
    （采集在工作日 17:40，后端没开的那几天会断档）。
    前端与问答都必须显式带上这个日期，否则会被误读成"今天的行情"。
    """
    as_of = datetime.combine(target_date, time.max) if target_date else None
    rows, d = get_latest_sectors(db, as_of=as_of)
    return {
        "date": d,
        "count": len(rows),
        "rows": [
            {
                "name": r.name,
                "change_pct": r.change_pct,
                "turnover": r.turnover,
                "company_count": r.company_count,
                "leader": r.leader,
            }
            for r in rows
        ],
    }
