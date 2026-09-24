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


def _to_date(v) -> datetime | None:
    """统一转 naive datetime；**取不到就返回 `None`**，不再拿当前时间冒充。

    ⚠️ 2026-09-25 改（第三方巡检 M6）。原实现在空值 / 解析失败时 `return datetime.now()`，
    后果**不是**报错，而是**更坏** —— 巡检报告说这会「唯一键冲突报错」，实测**不成立**：
    唯一键建在**完整 datetime** 上，而 `now()` 带时分秒微秒、真实行情是当日 00:00，
    两者时间戳不同 ⇒ **不冲突、不报错，两行并存**。再叠加两处「取最新」的消费者
    （`tools.get_market_overview` 的 `setdefault`、`report_service` 回测的 dict 覆盖）
    都是**按时间戳取最晚**，于是**坏行反而成了「最新行情」**，
    把真实的当日数据静默盖掉，还流进了问答与回测。

    凭空造一个「现在」既掩盖上游异常，又污染下游 —— 交 `None` 让调用方**丢弃并告警**。
    """
    if v is None:
        return None
    if hasattr(v, "to_pydatetime"):
        dt = v.to_pydatetime()
    elif isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = pd.to_datetime(v).to_pydatetime()
        except Exception:  # noqa: BLE001
            return None
    # ⚠️ **`NaT` 必须一起挡掉** —— 第一版只判了 `None`，实测直接被测试抓出来：
    # `pd.to_datetime('')` 与 `pd.to_datetime(nan)` **不抛异常**，返回 `NaT`，
    # 于是它会一路落库成非法日期。（顺带说明：原实现在「空字符串」这一支上
    # 返回的其实是 `NaT` 而不是 `datetime.now()` —— 是第三种失效形态。）
    if dt is None or pd.isna(dt):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


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
                # 日期取不到就**丢掉这一行并告警** —— 不许拿 `now()` 冒充
                # （见 `_to_date` 的注释：那会让坏行变成「最新行情」并盖掉真实数据）
                d = _to_date(row.get("date"))
                if d is None:
                    logger.warning(
                        f"[{symbol}] 行情日期缺失或无法解析，**丢弃该行**"
                        f"（原文 {row.get('date')!r}）—— 用当前时间冒充会把"
                        f"历史数据伪装成今天，且坏行会盖掉真实当日行情"
                    )
                    continue
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
                # 日期取不到就**丢掉这一行并告警** —— 不许拿 `now()` 冒充
                # （见 `_to_date` 的注释：那会让坏行变成「最新行情」并盖掉真实数据）
                d = _to_date(row.get("date"))
                if d is None:
                    logger.warning(
                        f"[{symbol}] 行情日期缺失或无法解析，**丢弃该行**"
                        f"（原文 {row.get('date')!r}）—— 用当前时间冒充会把"
                        f"历史数据伪装成今天，且坏行会盖掉真实当日行情"
                    )
                    continue
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
