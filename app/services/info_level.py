"""信息量评估：判断"今天有没有料"。

三个信号，**任一满足即视为有料**；全不满足则 low_info=True。

    ① 新颖度   —— 今日新增入库的新闻数（补跑时改按「今日发布」，见 use_publish_time）
    ② 事件强度 —— 今日新闻中存在多源佐证（source_count 高）
    ③ 市场异动 —— 任一主要指数涨跌幅超阈值

2026-09-22：原「②b 情绪强度」随新闻级情绪分一并删除。依据是实测近 5 天该信号
只触发 1 次，且情绪分覆盖率仅 13.6% —— 一个既不常触发、数据又残缺的信号。

设计立场：**low_info 只作标记，不阻止报告生成**。
"今天很平静"本身就是一条信息；且跳过会在时间序列上开洞，破坏回测与评估。
"""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.config import settings
from app.models.market import MarketData
from app.models.news import News


def assess_info_level(db: Session, target_date: date | None = None,
                      use_publish_time: bool = False) -> dict:
    """评估指定日期（默认今天）的信息量。

    `use_publish_time`：信号①改按**发布时间**统计。补跑时必须打开——
    补跑是"今天才把旧新闻采集进来"，`collected_at` 落在补跑当天而非目标日，
    按它统计信号①会**恒为 0**，让 `low_info` 偏保守。
    实时生成不传（默认 False），行为与改动前逐字段一致。
    """
    d = target_date or date.today()
    day_start = datetime.combine(d, time.min)
    day_end = datetime.combine(d, time.max)

    # ① 新颖度：今日新入库的新闻（数据采集 Agent 当天新增的事件数）。
    # ②a/②b 本来就用 publish_time，① 是唯一的例外——因为它要衡量的是
    # "今天的采集动作拿到了多少新东西"；但补跑场景下这个口径失效（见 docstring）。
    new_col = News.publish_time if use_publish_time else News.collected_at
    new_count = (
        db.query(News)
        .filter(new_col >= day_start, new_col <= day_end)
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

    # ③ 市场异动：任一主要指数涨跌幅绝对值
    rows = (
        db.query(MarketData)
        .filter(MarketData.date >= day_start, MarketData.date <= day_end)
        .all()
    )
    changes = [abs(r.change_pct) for r in rows if r.change_pct is not None]
    max_change = round(max(changes), 2) if changes else 0.0

    # 文案随口径切换——这些字会随 final 落进报告正文存档，
    # 补跑时写「新增」会与实际统计口径（发布）不符
    new_short = "发布" if use_publish_time else "新增"

    signals = {
        "new_count": {
            "value": new_count, "threshold": settings.info_new_threshold,
            "pass": new_count >= settings.info_new_threshold,
            "desc": f"今日{new_short}新闻数",
        },
        "multi_source": {
            "value": multi_source, "threshold": 1,
            "pass": multi_source >= 1,
            "desc": f"多源佐证新闻数（source_count≥{settings.info_source_threshold}）",
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
            f"三信号均未触发（{new_short}{new_count}条、多源{multi_source}条、"
            f"最大波动{max_change}%）"
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
