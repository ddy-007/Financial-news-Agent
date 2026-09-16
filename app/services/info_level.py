"""信息量评估：判断"今天有没有料"。

四个信号，**任一满足即视为有料**；全不满足则 low_info=True。

    ① 新颖度    —— 今日新增入库的新闻数
    ②a 事件强度 —— 今日新闻中存在多源佐证（source_count 高）
    ②b 情绪强度 —— 今日新闻中存在明确多空倾向（|sentiment| 大）※依赖情绪分覆盖
    ③ 市场异动  —— 任一主要指数涨跌幅超阈值

设计立场：**low_info 只作标记，不阻止报告生成**。
"今天很平静"本身就是一条信息；且跳过会在时间序列上开洞，破坏回测与评估。
"""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.market import MarketData
from app.models.news import News


def assess_info_level(db: Session, target_date: date | None = None) -> dict:
    """评估指定日期（默认今天）的信息量。"""
    d = target_date or date.today()
    day_start = datetime.combine(d, time.min)
    day_end = datetime.combine(d, time.max)

    # ① 新颖度：今日新入库的新闻（数据采集 Agent 当天新增的事件数）
    new_count = (
        db.query(News)
        .filter(News.collected_at >= day_start, News.collected_at <= day_end)
        .count()
    )

    # ②a 事件强度：今日发布的新闻中，有几个源以上报道的
    multi_source = (
        db.query(News)
        .filter(
            News.publish_time >= day_start,
            News.publish_time <= day_end,
            News.source_count >= settings.info_source_threshold,
        )
        .count()
    )

    # ②b 情绪强度：今日新闻中情绪倾向明确的（依赖情绪分覆盖）
    strong_sentiment = (
        db.query(News)
        .filter(
            News.publish_time >= day_start,
            News.publish_time <= day_end,
            News.sentiment.isnot(None),
            func.abs(News.sentiment) >= settings.info_sentiment_threshold,
        )
        .count()
    )

    # ③ 市场异动：任一主要指数涨跌幅绝对值
    rows = (
        db.query(MarketData)
        .filter(MarketData.date >= day_start, MarketData.date <= day_end)
        .all()
    )
    changes = [abs(r.change_pct) for r in rows if r.change_pct is not None]
    max_change = round(max(changes), 2) if changes else 0.0

    signals = {
        "new_count": {
            "value": new_count, "threshold": settings.info_new_threshold,
            "pass": new_count >= settings.info_new_threshold,
            "desc": "今日新增新闻数",
        },
        "multi_source": {
            "value": multi_source, "threshold": 1,
            "pass": multi_source >= 1,
            "desc": f"多源佐证新闻数（source_count≥{settings.info_source_threshold}）",
        },
        "strong_sentiment": {
            "value": strong_sentiment, "threshold": 1,
            "pass": strong_sentiment >= 1,
            "desc": f"强情绪新闻数（|sentiment|≥{settings.info_sentiment_threshold}）",
        },
        "market_move": {
            "value": max_change, "threshold": settings.info_market_threshold,
            "pass": max_change >= settings.info_market_threshold,
            "desc": "最大指数涨跌幅绝对值(%)",
        },
    }

    passed = [k for k, v in signals.items() if v["pass"]]
    low_info = len(passed) == 0
    if low_info:
        reason = (
            f"四信号均未触发（新增{new_count}条、多源{multi_source}条、"
            f"强情绪{strong_sentiment}条、最大波动{max_change}%）"
        )
    else:
        reason = "触发信号：" + "、".join(passed)

    return {
        "date": str(d),
        "low_info": low_info,
        "passed_signals": passed,
        "signals": signals,
        "reason": reason,
    }
