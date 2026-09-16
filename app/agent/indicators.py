"""技术指标计算（MA / RSI / 量比 / 动量）。

指标由代码算好再喂给 LLM，避免 LLM 自己算错。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.market import MarketData


def _ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return round(sum(values[-n:]) / n, 2)


def _rsi(closes: list[float], n: int = 14) -> float | None:
    """RSI（简单移动平均版）。"""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def compute_indicators(db: Session, symbol: str = "sh000001") -> dict:
    """计算某指数（默认上证）的技术指标。"""
    rows = (
        db.query(MarketData)
        .filter(MarketData.symbol == symbol)
        .order_by(MarketData.date.asc())
        .all()
    )
    closes = [r.close for r in rows if r.close is not None]
    volumes = [r.volume for r in rows if r.volume is not None]
    if len(closes) < 2:
        return {"available": False, "reason": "行情数据不足"}

    ma5, ma20, ma60 = _ma(closes, 5), _ma(closes, 20), _ma(closes, 60)
    last = closes[-1]
    result = {
        "available": True,
        "symbol": symbol,
        "last_close": last,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "rsi14": _rsi(closes),
        "momentum_5d": round((last / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None,
        "momentum_20d": round((last / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None,
        "volume_ratio": (
            round(volumes[-1] / (sum(volumes[-6:-1]) / 5), 2)
            if len(volumes) >= 6 and sum(volumes[-6:-1]) > 0 else None
        ),
        "days": len(closes),
    }

    # 多空排列描述（喂给 LLM 的定性结论），均线不足时降级
    mas = [(n, v) for n, v in (("MA5", ma5), ("MA20", ma20), ("MA60", ma60)) if v]
    if len(mas) >= 2:
        above = [n for n, v in mas if last > v]
        below = [n for n, _ in mas if n not in above]
        if not below:
            result["trend"] = f"多头排列（价格站上全部均线：{'、'.join(n for n, _ in mas)}）"
        elif not above:
            result["trend"] = f"空头排列（价格跌破全部均线：{'、'.join(n for n, _ in mas)}）"
        else:
            result["trend"] = f"震荡（站上 {'、'.join(above)}，跌破 {'、'.join(below)}）"
    elif len(mas) == 1:
        n, v = mas[0]
        result["trend"] = f"数据不足（仅可算 {n}，价格{'高于' if last > v else '低于'}该均线）"
    return result


def format_indicators(ind: dict) -> str:
    """把指标 dict 格式化为给 LLM 阅读的文本。"""
    if not ind.get("available"):
        return f"技术指标不可用：{ind.get('reason', '未知原因')}"
    lines = [
        f"最新收盘：{ind['last_close']}",
        f"MA5={ind.get('ma5')}　MA20={ind.get('ma20')}　MA60={ind.get('ma60')}",
        f"RSI14={ind.get('rsi14')}",
        f"5日动量={ind.get('momentum_5d')}%　20日动量={ind.get('momentum_20d')}%",
        f"量比（当日/前5日均量）={ind.get('volume_ratio')}",
        f"形态：{ind.get('trend', '未知')}",
    ]
    return "\n".join(lines)
