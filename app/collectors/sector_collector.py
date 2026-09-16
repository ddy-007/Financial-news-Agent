"""行业板块数据采集。

**为什么不直接用东财接口**（`ak.stock_board_industry_name_em`）：
该接口本身可用（实测能返回 496 个板块），但东财会**按 IP 限流**——
短时间高频请求后会被临时拒绝（实测失败后等待 90 秒仍未恢复）。
而板块数据是**日频**的，一天只需取一次，高频请求毫无必要。

因此本模块的策略：
  1. **本地缓存一天**——从根本上避免触发限流（这是主要的修复手段）
  2. **主源用新浪**（`stock_sector_spot`）——实测稳定，不与其他模块争抢东财配额
  3. **统一走重试**——应对偶发网络抖动（注意：重试**不能**解决限流）

注意：重试对限流无效（退避 1~2 秒远短于限流窗口），所以不要靠重试硬扛，
     要靠"减少请求次数"。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from loguru import logger

from app.retry import call_with_retry

CACHE_PATH = Path("data/sector_cache.json")


def _read_cache() -> list[dict] | None:
    """读当日缓存。过期或损坏返回 None。"""
    try:
        if not CACHE_PATH.exists():
            return None
        with CACHE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != date.today().isoformat():
            return None
        sectors = data.get("sectors")
        return sectors if isinstance(sectors, list) and sectors else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"板块缓存读取失败（将重新拉取）: {e}")
        return None


def _write_cache(sectors: list[dict]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": date.today().isoformat(),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": "sina",
            "count": len(sectors),
            "sectors": sectors,
        }
        with CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        logger.info(f"板块数据已缓存到 {CACHE_PATH}，共 {len(sectors)} 个板块")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"板块缓存写入失败（不影响本次使用）: {e}")


def _fetch_from_sina() -> list[dict]:
    """新浪行业板块。实测稳定，且不与新闻/行情模块争抢东财配额。"""
    import akshare as ak

    df = call_with_retry(
        ak.stock_sector_spot, indicator="行业", retry_label="行业板块(新浪)"
    )
    if df is None or df.empty:
        return []
    sectors = []
    for _, row in df.iterrows():
        name = _str(row.get("板块"))
        if not name:
            continue
        sectors.append({
            "name": name,
            "change_pct": _num(row.get("涨跌幅")),
            "avg_price": _num(row.get("平均价格")),
            "volume": _num(row.get("总成交量")),
            "turnover": _num(row.get("总成交额")),
            "company_count": _num(row.get("公司家数")),
            "leader": _str(row.get("股票名称")),
        })
    # 按涨跌幅降序（None 排最后）——源数据是按板块代码排的，不排序则无法直接看出领涨/领跌
    sectors.sort(key=lambda s: (s["change_pct"] is None, -(s["change_pct"] or 0.0)))
    return sectors


def _num(v):
    """安全转数值：NaN / 空 / 非数字 → None。"""
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else round(f, 4)  # NaN != NaN
    except (TypeError, ValueError):
        return None


def _str(v) -> str | None:
    """安全转字符串。

    注意：pandas 的空单元格是 NaN，而 `NaN or ""` 仍返回 NaN（NaN 是真值），
    `str(NaN)` 会得到字符串 "nan" 并被写进库或 prompt。故必须显式过滤。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def fetch_industry_sectors(use_cache: bool = True) -> dict:
    """取行业板块数据。默认走当日缓存。

    返回：{"date":..., "source":..., "count":..., "sectors":[...], "from_cache":bool}
    """
    if use_cache:
        cached = _read_cache()
        if cached:
            logger.info(f"板块数据：使用当日缓存，共 {len(cached)} 个板块")
            return {
                "date": date.today().isoformat(), "source": "cache",
                "count": len(cached), "sectors": cached, "from_cache": True,
            }

    sectors = _fetch_from_sina()
    if sectors:
        _write_cache(sectors)
        return {
            "date": date.today().isoformat(), "source": "sina",
            "count": len(sectors), "sectors": sectors, "from_cache": False,
        }

    logger.warning("板块数据获取失败（新浪源返回空）")
    return {
        "date": date.today().isoformat(), "source": None,
        "count": 0, "sectors": [], "from_cache": False,
    }
