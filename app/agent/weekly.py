"""周度综述：聚合本周日报，生成周报。

定位：日报负责「记录」，周报负责「判断」——趋势只有放到一周尺度才看得出来。
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.agent.experts import extract_json
from app.agent.llm import get_llm, get_llm_model_name
from app.config import settings
from app.models.market import MarketData
from app.models.report import MarketReport

WEEKLY_PROMPT = """你是一位首席策略师，负责撰写本周的 A 股周度综述。

【本周行情】（各指数周初→周末）
{market_week}

【本周每日研判的定调】
{daily_series}

【本周专家观点的演变】（首次 → 末次）
{expert_trend}

【本周风险官的风险等级】
{risk_series}

【本周累计的主要驱动因素】
{drivers}

【本周累计的风险提示】
{risks}

撰写要求：
1. **突出本周整体的变化趋势**，不要罗列每天发生了什么
2. 指出本周市场的主线逻辑是什么
3. 说明专家观点在本周内是否发生了转向（如有）
4. 风险提示要完整保留，不得淡化
5. 结论仅供参考，不构成投资建议

严格只输出 JSON（不要输出任何其他文字）：
{{
  "market_summary": "本周综述",
  "key_drivers": ["本周主线驱动"],
  "sector_opportunities": ["值得关注的板块"],
  "risks": ["下周需警惕的风险"],
  "consensus_note": "本周专家观点的演变与分歧"
}}
"""


def _week_range(d: date) -> tuple[datetime, datetime]:
    """返回 d 所在自然周的 [周一 00:00, 周日 23:59:59]。"""
    monday = d - timedelta(days=d.weekday())
    return (datetime.combine(monday, time.min),
            datetime.combine(monday + timedelta(days=6), time.max))


def _load_dailies(db: Session, start: datetime, end: datetime) -> list[MarketReport]:
    return (
        db.query(MarketReport)
        .filter(
            MarketReport.date >= start,
            MarketReport.date <= end,
            (MarketReport.report_type == "daily") | (MarketReport.report_type.is_(None)),
        )
        .order_by(MarketReport.date.asc())
        .all()
    )


def _parse(r: MarketReport) -> dict:
    try:
        c = json.loads(r.content) if r.content else {}
    except (json.JSONDecodeError, TypeError):
        c = {}
    if not isinstance(c, dict):
        c = {}
    try:
        experts = json.loads(r.expert_opinions) if r.expert_opinions else []
    except (json.JSONDecodeError, TypeError):
        experts = []
    return {"content": c, "experts": experts if isinstance(experts, list) else []}


def _market_week(db: Session, start: datetime, end: datetime) -> str:
    """本周各指数的首末收盘与区间涨跌幅。"""
    rows = (
        db.query(MarketData)
        .filter(MarketData.date >= start, MarketData.date <= end)
        .order_by(MarketData.date.asc())
        .all()
    )
    by_symbol: dict = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, []).append(r)
    lines = []
    for sym, items in by_symbol.items():
        if len(items) < 2:
            continue
        first, last = items[0], items[-1]
        if first.close and last.close:
            pct = (last.close - first.close) / first.close * 100
            lines.append(
                f"{last.name}({sym}): {first.close} → {last.close}（{pct:+.2f}%）"
            )
    return "\n".join(lines) if lines else "无"


def _dedup_merge(dailies: list[dict], key: str, limit: int = 12) -> str:
    seen, out = set(), []
    for d in dailies:
        for item in (d["content"].get(key) or []):
            s = str(item).strip()
            k = s[:20]
            if s and k not in seen:
                seen.add(k)
                out.append(s)
    return "\n".join(f"- {x}" for x in out[:limit]) if out else "无"


def generate_weekly_report(db: Session,
                           end_date: date | None = None) -> MarketReport | None:
    """聚合本周日报生成周报。日报不足 2 份时返回 None。"""
    d = end_date or date.today()
    start, end = _week_range(d)

    rows = _load_dailies(db, start, end)

    # 风险否决必须在**去重之前**用全体日报算：`any()` 是布尔或，本就不受
    # 重复影响；先去重反而可能丢掉只出现在当天较早那份上的 `veto=True`。
    risk_veto = any(r.risk_veto for r in rows)

    # 按天去重：优先保留当天**有 score** 的最后一份，若全天无 score 则保留
    # date 最晚的那份。与 `compute_backtest()` 同一口径。
    # 为什么要去重：均分与分歧度是**平均值**，同一天因手动重跑留下多份会重复加权。
    # 排序键 `(date, score is not None)`：同一天里无分的排前、有分的排后，
    # dict 覆盖后留下的即「最后一份有分的」。
    _by_day: dict = {}
    for r in sorted(rows, key=lambda r: (r.date, r.score is not None)):
        _by_day[r.date.date()] = r
    rows = list(_by_day.values())

    if len(rows) < 2:
        logger.info(f"本周日报仅 {len(rows)} 份（需 ≥2），不生成周报")
        return None

    parsed = [_parse(r) for r in rows]

    # 定调序列
    daily_series = "\n".join(
        f"- {r.date.date()} {r.sentiment}（score={r.score if r.score is not None else 0:+.2f}）"
        for r in rows
    )

    # 专家观点演变：每位专家 首次 → 末次
    trend_lines = []
    names = ["宏观", "行业", "资金面", "技术面"]
    for name in names:
        seq = []
        for p in parsed:
            for o in p["experts"]:
                if o.get("expert") == name:
                    seq.append(f"{o.get('stance')}({o.get('score', 0):+.1f})")
        if seq:
            arrow = f"{seq[0]} → {seq[-1]}" if seq[0] != seq[-1] else f"{seq[0]}（未变）"
            trend_lines.append(f"- {name}：{arrow}")
    expert_trend = "\n".join(trend_lines) if trend_lines else "无"

    # 风险等级序列
    risk_levels = [
        f"- {r.date.date()}: {((p['content'].get('risk_opinion') or {}).get('risk_level') or '未知')}"
        for r, p in zip(rows, parsed)
    ]

    # 量化汇总
    scores = [r.score for r in rows if r.score is not None]
    weekly_score = sum(scores) / len(scores) if scores else 0.0
    # ⚠️ **必须走配置**，不写死 0.15（2026-09-25 修）。
    # 这里是全项目**第 4 个**用「中性带」的地方（前三处：`aggregate_node` 定调 /
    # `compute_backtest` 判方向 / `_score_bucket` 分档），而 `config.py` 的注释
    # 明写「三处共用这一套值，不许各写各的」。周报这处漏了 —— 改配置后
    # 日报与周报会对**同一个分数**给出不同方向。
    _band = settings.score_neutral_band
    sentiment = ("偏多" if weekly_score > _band
                 else ("偏空" if weekly_score < -_band else "中性"))
    divs = [r.divergence for r in rows if r.divergence is not None]
    divergence = round(sum(divs) / len(divs), 3) if divs else None
    # risk_veto 已在上面（去重之前）算好

    # LLM 撰写
    llm = get_llm(temperature=0.2, part="weekly")
    prompt = WEEKLY_PROMPT.format(
        market_week=_market_week(db, start, end),
        daily_series=daily_series,
        expert_trend=expert_trend,
        risk_series="\n".join(risk_levels),
        drivers=_dedup_merge(parsed, "key_drivers"),
        risks=_dedup_merge(parsed, "risks"),
    )
    try:
        data = extract_json(llm.invoke(prompt).content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"周报 LLM 生成失败: {e}")
        data = None
    if not isinstance(data, dict):
        data = {"market_summary": "（周报生成失败）", "key_drivers": [],
                "sector_opportunities": [], "risks": [], "consensus_note": ""}

    # 强制保留本周全部风险提示（不得被 LLM 过滤）
    weekly_risks = [str(x) for x in (data.get("risks") or [])]
    for p in parsed:
        for item in (p["content"].get("risks") or []):
            s = str(item).strip()
            if s and s not in weekly_risks:
                weekly_risks.append(s)

    final = {
        "week_start": str(start.date()),
        "week_end": str(end.date()),
        "market_summary": str(data.get("market_summary", "")),
        "sentiment": sentiment,
        "score": round(weekly_score, 3),
        "key_drivers": data.get("key_drivers") or [],
        "sector_opportunities": data.get("sector_opportunities") or [],
        "risks": weekly_risks,
        "consensus_note": str(data.get("consensus_note", "")),
        "daily_count": len(rows),
        "daily_series": [
            {"date": str(r.date.date()), "sentiment": r.sentiment, "score": r.score}
            for r in rows
        ],
        "divergence": divergence,
        "risk_veto": risk_veto,
    }

    # 落 23:59:59：日报落在当天 REPORT_TIME（更早），错开时刻以便按 date 区分两份报告。
    # 这里**不写死具体时刻** —— 日报时间是可配的，写死必然过期。
    report_date = datetime.combine(end.date(), time(23, 59, 59))

    # 同一周已有周报则**覆盖更新**：避免 date 唯一约束冲突，也允许调度器刷新
    existing = (
        db.query(MarketReport)
        .filter(
            MarketReport.report_type == "weekly",
            MarketReport.date >= start,
            MarketReport.date <= end,
        )
        .first()
    )
    if existing:
        report = existing
        logger.info(f"本周已有周报，覆盖更新（原 date={report.date}）")
    else:
        report = MarketReport(date=report_date, report_type="weekly")
        db.add(report)

    # H2（2026-09-25）：业务日 —— 周报按它的 `report_date` 记。
    # 唯一索引是 `(report_type, report_day)`，不设的话这一列会一直是 NULL，
    # 而 **SQLite 的唯一索引认为 NULL 互不相等**，等于在索引上留了个洞。
    report.report_day = report_date.date()

    report.title = str(final["market_summary"])[:200]
    report.content = json.dumps(final, ensure_ascii=False, indent=2)
    report.sentiment = sentiment
    report.confidence = "medium"
    report.score = round(weekly_score, 3)
    report.expert_opinions = None
    report.divergence = divergence
    report.risk_veto = risk_veto
    report.model = get_llm_model_name(part="weekly")

    db.commit()
    db.refresh(report)
    logger.info(
        f"周报生成完成：{start.date()}~{end.date()} "
        f"{len(rows)}份日报 周定调={sentiment} score={weekly_score:+.2f}"
    )
    return report
