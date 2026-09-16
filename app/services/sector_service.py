"""板块数据业务：采集入库 + 供研判使用的摘要格式化。"""
from __future__ import annotations

from datetime import date

from loguru import logger
from sqlalchemy.orm import Session

from app.models.sector import SectorData


def save_sectors(db: Session, data: dict, target_date: date | None = None) -> int:
    """入库。已有同 (日期, 板块名) 记录则**更新**，返回写入条数。

    用"查-改-增"而非直接 insert，避免重复采集时触发唯一约束错误。
    """
    d = target_date or date.today()
    rows = data.get("sectors") or []
    if not rows:
        return 0

    existing = {
        s.name: s
        for s in db.query(SectorData).filter(SectorData.date == _day_start(d)).all()
    }
    written = 0
    seen: set[str] = set()
    for item in rows:
        name = item.get("name")
        # 批内去重：源数据若含重名板块，两次 db.add 会在 commit 时触发唯一约束、整批回滚
        if not name or name in seen:
            continue
        seen.add(name)
        row = existing.get(name)
        if row is None:
            row = SectorData(date=_day_start(d), name=name)
            db.add(row)
        row.change_pct = item.get("change_pct")
        row.avg_price = item.get("avg_price")
        row.volume = item.get("volume")
        row.turnover = item.get("turnover")
        row.company_count = item.get("company_count")
        row.leader = item.get("leader")
        row.source = data.get("source")
        written += 1
    db.commit()
    return written


def _day_start(d: date):
    from datetime import datetime, time

    return datetime.combine(d, time.min)


def collect_and_store_sectors(db: Session) -> int:
    """采集 + 入库（供调度器调用）。走日缓存，不会触发东财限流。"""
    from app.collectors.sector_collector import fetch_industry_sectors

    data = fetch_industry_sectors()
    n = save_sectors(db, data)
    logger.info(f"板块数据入库 {n} 条（来源 {data.get('source')}）")
    return n


def get_latest_sectors(db: Session) -> tuple[list[SectorData], str | None]:
    """取库中最新一天的板块数据，返回 (记录列表, 日期字符串)。"""
    latest = (
        db.query(SectorData.date).order_by(SectorData.date.desc()).first()
    )
    if not latest:
        return [], None
    d = latest[0]
    rows = (
        db.query(SectorData)
        .filter(SectorData.date == d)
        .order_by(SectorData.change_pct.desc())
        .all()
    )
    return rows, d.date().isoformat()


def format_sector_summary(db: Session, top_n: int = 8) -> str:
    """格式化为**给分析师阅读**的板块摘要。

    只给结论性的头部信息（领涨/领跌/涨跌家数），不堆全量数据——
    84 个板块全塞进 prompt 只会稀释注意力。
    """
    rows, d = get_latest_sectors(db)
    if not rows:
        return "无"

    ranked = [r for r in rows if r.change_pct is not None]
    if not ranked:
        return "无"

    up = sum(1 for r in ranked if r.change_pct > 0)
    down = sum(1 for r in ranked if r.change_pct < 0)

    # 板块数不足时收窄 top_n，否则"领涨"与"领跌"两行会互相包含、自相矛盾
    n = max(1, min(top_n, len(ranked) // 2)) if len(ranked) > 1 else 1

    def fmt(r: SectorData, show_leader: bool) -> str:
        # 只在**上涨**板块标注领涨股——对下跌板块说"领涨"自相矛盾
        leader = f"（领涨 {r.leader}）" if (show_leader and r.leader) else ""
        return f"{r.name} {r.change_pct:+.2f}%{leader}"

    stale_note = "" if d == date.today().isoformat() else "，**非今日数据**"
    lines = [
        f"（数据日期 {d}{stale_note}，共 {len(ranked)} 个板块）",
        "领涨：" + "、".join(fmt(r, True) for r in ranked[:n]),
        "领跌：" + "、".join(fmt(r, False) for r in reversed(ranked[-n:])),
        f"全市场：{up} 涨 / {down} 跌",
    ]
    return "\n".join(lines)
