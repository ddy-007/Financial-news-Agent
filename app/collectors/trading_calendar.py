"""A 股交易日历：供调度器判定"今天股市开不开门"。

**按年缓存**：交易日历本质是"年历"（国务院提前公布放假安排），
故一年只需联网拉取一次，之后直接从本地缓存读——彻底消除
"网络抖动导致降级"的问题。

加载顺序：
    1. 本地缓存覆盖当年 → 直接使用（离线可用）
    2. 缓存过期/缺失 → 联网拉取并写入缓存
    3. 拉取失败 → 降级为「工作日」判断，并置降级标记供下游标注

⚠️ **绝不使用"过期日历"兜底**：上一年度的日历中不含本年度的任何日期，
   会把整年交易日判成休市，导致系统整年不工作——比降级为工作日判断糟糕得多。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger

_dates: set[date] | None = None
_loaded = False
_degraded = False                # 是否处于"工作日"降级状态
_last_fail: datetime | None = None
RETRY_COOLDOWN = timedelta(minutes=30)  # 拉取失败后的冷却期，避免网络故障时反复卡顿
CACHE_PATH = Path("data/trade_calendar.json")


def _read_cache() -> dict | None:
    """读本地缓存。返回 None 表示无缓存或损坏。"""
    try:
        if not CACHE_PATH.exists():
            return None
        with CACHE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("dates") else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"交易日历缓存读取失败（将重新拉取）: {e}")
        return None


def _write_cache(dates: set[date]) -> None:
    """写本地缓存（含覆盖年份，用于判断是否过期）。"""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "covers_year": max(d.year for d in dates),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(dates),
            "dates": sorted(d.isoformat() for d in dates),
        }
        with CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        logger.info(f"交易日历已缓存到 {CACHE_PATH}（覆盖至 {payload['covers_year']} 年）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"交易日历缓存写入失败（不影响本次使用）: {e}")


def _load() -> set[date] | None:
    """加载交易日历。返回 None 表示当前不可用（走降级判断）。"""
    global _dates, _loaded, _degraded, _last_fail
    if _loaded:
        return _dates

    this_year = date.today().year

    # 1. 本地缓存（覆盖当年即可用）
    cached = _read_cache()
    if cached:
        # 全部解析放进 try：缓存文件可能被外部改坏（covers_year 为 null / 非数字）
        # 此处若抛错会冒到 _warm_up → lifespan，导致应用起不来
        try:
            covers = int(cached.get("covers_year") or 0)
        except (TypeError, ValueError):
            covers = 0
            logger.warning("交易日历缓存 covers_year 非法，视为过期")
        if covers >= this_year:
            try:
                _dates = {date.fromisoformat(s) for s in cached["dates"]}
                _loaded = True
                _degraded = False
                logger.info(
                    f"交易日历：本地缓存加载 {len(_dates)} 个交易日"
                    f"（覆盖至 {cached['covers_year']} 年，拉取于 {cached.get('fetched_at')}）"
                )
                return _dates
            except Exception as e:  # noqa: BLE001
                logger.warning(f"交易日历缓存解析失败（将重新拉取）: {e}")

    # 2. 冷却期内不重复联网
    if _last_fail and datetime.now() - _last_fail < RETRY_COOLDOWN:
        _degraded = True
        return None

    # 3. 联网拉取
    try:
        import akshare as ak
        import pandas as pd

        df = ak.tool_trade_date_hist_sina()
        dates = {pd.Timestamp(x).date() for x in df["trade_date"]}
        if not dates:
            # 空表不是"没有交易日"，是数据源异常——必须走失败分支，否则会静默判全天休市
            raise ValueError("交易日历数据源返回空表")
        _write_cache(dates)
        _dates = dates
        _loaded = True
        _degraded = False
        _last_fail = None
        logger.info(f"交易日历：联网拉取 {len(dates)} 个交易日，已缓存到本地")
    except Exception as e:  # noqa: BLE001
        _last_fail = datetime.now()
        _dates = None
        _degraded = True
        logger.warning(
            f"交易日历拉取失败，临时降级为「工作日」判断"
            f"（{int(RETRY_COOLDOWN.total_seconds() // 60)} 分钟后重试）: {e}"
        )
    return _dates


def status() -> dict:
    """交易日历当前状态（**只读**，不触发加载/联网/写盘）。

    `loaded=False` 表示"尚未检查过"，与"检查过且正常"是两回事——
    只看 `degraded` 会把"未知"误报成"健康"。
    """
    return {"loaded": _loaded, "degraded": _degraded}


def is_degraded(ensure_loaded: bool = False) -> bool:
    """交易日历是否处于降级状态（联网失败，正在按"工作日"猜测）。

    `ensure_loaded=True` 会触发加载（可能联网并写缓存），供启动预热使用；
    默认 False 只读已知状态，供**评估层等只读场景**使用，避免副作用。
    """
    if ensure_loaded:
        _load()
    return _degraded


def is_trading_day(d: date | datetime | None = None) -> bool:
    """今天（或指定日）是否为 A 股交易日。"""
    if isinstance(d, datetime):
        d = d.date()
    d = d or date.today()
    dates = _load()
    if dates is None:
        return d.weekday() < 5  # 降级：仅排除周末
    return d in dates


def is_last_trading_day_of_week(d: date | datetime | None = None) -> bool:
    """今天（或指定日）是否为本周最后一个交易日。

    用于周报触发：自动适配假期缩短的周（如周五休市 → 周四即为本周最后交易日）。
    """
    if isinstance(d, datetime):
        d = d.date()
    d = d or date.today()
    if not is_trading_day(d):
        return False
    iso = d.isocalendar()[:2]
    for i in range(1, 8):
        nd = d + timedelta(days=i)
        if nd.isocalendar()[:2] != iso:
            break
        if is_trading_day(nd):
            return False
    return True
