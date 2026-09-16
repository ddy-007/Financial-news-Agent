"""行情采集：AkShare（A股指数）+ yfinance（美股指数）。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from loguru import logger

from app.retry import call_with_retry

A_SHARE_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
]

US_INDICES = [
    (".INX", "标普500"),
    (".DJI", "道琼斯"),
    (".IXIC", "纳斯达克"),
]

# 每个指数回采的历史天数（用于前端折线图 + MA60 等技术指标）
HISTORY_DAYS = 70


def _f(v) -> float | None:
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_date(v) -> datetime:
    """统一转 naive datetime。"""
    if v is None:
        return datetime.now()
    if hasattr(v, "to_pydatetime"):
        dt = v.to_pydatetime()
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    try:
        return pd.to_datetime(v).to_pydatetime()
    except Exception:
        return datetime.now()


def _pct(last_close: float | None, prev_close: float | None) -> float | None:
    if last_close is None or prev_close in (None, 0):
        return None
    return round((last_close - prev_close) / prev_close * 100, 2)


def collect_a_share_indices() -> list[dict]:
    import akshare as ak

    rows = []
    for symbol, name in A_SHARE_INDICES:
        try:
            df = call_with_retry(ak.stock_zh_index_daily, symbol=symbol,
                                 retry_label=f"A股指数 {name}")
            if df is None or df.empty:
                continue
            df = df.tail(HISTORY_DAYS)  # 最近 N 天，用于折线图
            prev_close: float | None = None
            for _, row in df.iterrows():
                close = _f(row["close"])
                rows.append({
                    "symbol": symbol,
                    "name": name,
                    "date": _to_date(row.get("date")),
                    "open": _f(row.get("open")),
                    "high": _f(row.get("high")),
                    "low": _f(row.get("low")),
                    "close": close,
                    "volume": _f(row.get("volume")),
                    "change_pct": _pct(close, prev_close),
                    "turnover": None,
                })
                prev_close = close
        except Exception as e:  # noqa: BLE001
            logger.warning(f"A股指数 {name}({symbol}) 采集失败: {e}")
    logger.info(f"A股指数采集完成 {len(rows)} 条")
    return rows


def collect_us_indices() -> list[dict]:
    """美股指数：AkShare 新浪源（yfinance 在中国大陆不可用）。"""
    import akshare as ak

    rows = []
    for symbol, name in US_INDICES:
        try:
            df = call_with_retry(ak.index_us_stock_sina, symbol=symbol,
                                 retry_label=f"美股指数 {name}")
            if df is None or df.empty:
                continue
            df = df.tail(HISTORY_DAYS)  # 最近 N 天
            prev_close: float | None = None
            for _, row in df.iterrows():
                close = _f(row["close"])
                rows.append({
                    "symbol": symbol,
                    "name": name,
                    "date": _to_date(row.get("date")),
                    "open": _f(row.get("open")),
                    "high": _f(row.get("high")),
                    "low": _f(row.get("low")),
                    "close": close,
                    "volume": _f(row.get("volume")),
                    "change_pct": _pct(close, prev_close),
                    "turnover": None,
                })
                prev_close = close
        except Exception as e:  # noqa: BLE001
            logger.warning(f"美股指数 {name}({symbol}) 采集失败: {e}")
    logger.info(f"美股指数采集完成 {len(rows)} 条")
    return rows


def collect_all_market_data() -> list[dict]:
    return collect_a_share_indices() + collect_us_indices()
