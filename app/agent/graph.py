"""分析研判 Agent：LangGraph 多专家编排。

流程：
    prepare → [宏观|行业|资金面|技术面 四路并行] → 风险官(需看到分析师结论) → aggregate → 首席 → 报告

设计说明：
- 4 位分析师**并行且互不可见**，保证观点独立
- 风险官在分析师之后运行（它的价值在于**反驳具体观点**，必须看到结论）
- 定量结论（加权分/分歧度/置信度）由代码计算；LLM 只撰写叙述性内容
"""
from __future__ import annotations

import json
import operator
from datetime import date, datetime, timedelta
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from loguru import logger
from sqlalchemy.orm import Session

from app.agent.experts import (
    run_capital_expert,
    run_chief,
    run_industry_expert,
    run_macro_expert,
    run_risk_officer,
    run_technical_expert,
)
from app.agent.indicators import compute_indicators, format_indicators
from app.agent.llm import get_llm, get_llm_model_name, llm_retry_times
from app.agent.prompts import REPORT_PROMPT_TEMPLATE
from app.agent.schemas import ExpertOpinion, RiskOpinion
from app.config import settings
from app.retry import call_with_retry
from app.db import SessionLocal
from app.models.market import MarketData
from app.models.news import News
from app.models.report import MarketReport

NEWS_PER_CATEGORY = 8   # 每位专家读取的新闻条数
ALL_NEWS_LIMIT = 40     # 给风险官的全部新闻条数
CONF_LADDER = ["low", "medium", "high"]

# 专家权重（风险官不参与方向加权）
EXPERT_WEIGHTS = {
    "宏观": settings.weight_macro,
    "行业": settings.weight_industry,
    "资金面": settings.weight_capital,
    "技术面": settings.weight_technical,
}


class AnalystState(TypedDict, total=False):
    # 补跑用的「截止时点」（ISO 字符串）。**实时生成时不传**——此时全程走
    # datetime.now()，行为与引入本字段前完全一致。补跑必须传，否则流程会
    # 拿"今天"的数据去写"过去某天"的报告，回测时等于用未来信息预测过去。
    as_of: str
    ctx: dict
    info_level: dict                             # 信息量评估结果（prepare 阶段产出）
    data_freshness: dict                         # 数据时效（是否走了陈旧回退）
    env: dict                                    # 运行环境（如交易日历是否降级）
    opinions: Annotated[list, operator.add]      # 4 位分析师并行写入，自动合并
    failed: Annotated[list, operator.add]        # 失败的专家名
    risk_opinion: dict
    quant: dict
    final: dict


# ================= 数据准备 =================
def _fmt_news(rows) -> str:
    if not rows:
        return "无"
    return "\n".join(f"- [{r.source}] {r.title}（{r.source_count}源）" for r in rows)


def _fetch_news(db: Session, cats: list[str], limit: int,
                since: datetime, until: datetime | None = None
                ) -> tuple[list, bool]:
    """按类别取近期新闻。

    近 1 天无数据时回退到最新若干条（保证可跑），并返回 `stale=True`，
    以便下游**标注「数据陈旧」**——不再静默替换数据源。

    `until` 不为空时只取该时点**及之前**的新闻（补跑用）。
    **回退分支同样受 `until` 约束**——否则补跑时会捞到"未来"的新闻。
    """
    def _query(with_since: bool):
        q = db.query(News).filter(News.category.in_(cats))
        if with_since:
            q = q.filter(News.publish_time >= since)
        if until is not None:
            q = q.filter(News.publish_time <= until)
        return q.order_by(News.publish_time.desc()).limit(limit).all()

    rows = _query(with_since=True)
    if rows:
        return rows, False
    rows = _query(with_since=False)
    return rows, bool(rows)


def _calendar_degraded() -> bool:
    """日历是否降级（只读，不触发加载/联网）。"""
    from app.collectors.trading_calendar import is_degraded

    return is_degraded()


def _market_snapshot(db: Session, as_of: datetime | None = None) -> tuple[str, str]:
    """返回 (A股行情, 美股行情)。

    `as_of` 不为空时只取该时点**及之前**的行情（补跑用）；
    为空时行为与改动前完全一致（取全库最新）。
    """
    q = db.query(MarketData)
    if as_of is not None:
        q = q.filter(MarketData.date <= as_of)
    rows = q.order_by(MarketData.date.desc()).all()
    latest: dict = {}
    for r in rows:
        latest.setdefault(r.symbol, r)
    a_share, us = [], []
    for r in latest.values():
        line = f"{r.name}({r.symbol}): 收盘 {r.close}，涨跌幅 {r.change_pct}%（{r.date.date()}）"
        (us if r.symbol.startswith(".") else a_share).append(line)
    return ("\n".join(a_share) or "无", "\n".join(us) or "无")


def _safe_extra(fn, default, label: str):
    """**增量数据**的安全获取：失败只降级为缺省值，不拖垮核心研判流程。

    核心数据（新闻、行情）失败时整份报告降级是合理的；
    但板块表现、技术指标、信息量评估属于**增量增强**——
    它们缺失时研判仍应正常进行，不该让整份报告变成"降级模式"。
    """
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[prepare] {label} 获取失败，按缺省处理: {e}")
        return default


def prepare_node(state: AnalystState) -> dict:
    """组装所有专家需要的输入（含「综合」类新闻，用于广播）。

    `state["as_of"]`（可选）是补跑的截止时点。**不传时全程用 `datetime.now()`，
    行为与引入该字段前逐字节一致**——这是硬性要求，实时生成走的就是这条路径。
    """
    raw_as_of = state.get("as_of")
    as_of = datetime.fromisoformat(raw_as_of) if raw_as_of else None
    db = SessionLocal()
    try:
        since = (as_of or datetime.now()) - timedelta(days=1)
        macro, st1 = _fetch_news(db, ["宏观", "政策"], NEWS_PER_CATEGORY, since, as_of)
        industry, st2 = _fetch_news(db, ["行业", "公司"], NEWS_PER_CATEGORY, since, as_of)
        capital, st3 = _fetch_news(db, ["资金", "市场"], NEWS_PER_CATEGORY, since, as_of)
        comprehensive, st4 = _fetch_news(db, ["综合"], 10, since, as_of)

        q = db.query(News).filter(News.publish_time >= since)
        if as_of is not None:
            q = q.filter(News.publish_time <= as_of)
        all_news = q.order_by(News.publish_time.desc()).limit(ALL_NEWS_LIMIT).all()
        if not all_news:
            # 回退分支同样要受 as_of 约束，否则补跑会捞到"未来"的新闻
            q2 = db.query(News)
            if as_of is not None:
                q2 = q2.filter(News.publish_time <= as_of)
            all_news = (
                q2.order_by(News.publish_time.desc()).limit(ALL_NEWS_LIMIT).all()
            )
        a_share, us = _market_snapshot(db, as_of)
        # 三项增量数据均加保护：任一失败只按缺省处理，不拖垮核心研判
        from app.services.info_level import assess_info_level
        from app.services.sector_service import format_sector_summary

        indicators = _safe_extra(
            lambda: format_indicators(compute_indicators(db, as_of=as_of)),
            "技术指标不可用（获取失败）", "技术指标",
        )
        info_level = _safe_extra(
            lambda: assess_info_level(
                db,
                target_date=as_of.date() if as_of else None,
                # 补跑时信号①改按发布时间——否则 collected_at 落在补跑当天，
                # 对目标日恒为 0，low_info 会偏保守
                use_publish_time=as_of is not None,
            ),
            {}, "信息量评估")
        # 板块实际表现：给行业分析师做「新闻 vs 市场反应」的交叉验证
        sector_summary = _safe_extra(
            lambda: format_sector_summary(db, as_of=as_of), "无", "板块数据"
        )

        # 数据时效：**按类目分别记录**——若只取全局最大值，会出现
        # "只有一类陈旧、却对外声称基于当天新闻"的自相矛盾提示
        groups = {
            "宏观/政策": (macro, st1),
            "行业/公司": (industry, st2),
            "资金/市场": (capital, st3),
            "综合": (comprehensive, st4),
        }
        used = [r for rows, _ in groups.values() for r in rows]
        newest = max((n.publish_time for n in used if n.publish_time), default=None)

        stale_cats = [k for k, (_, st) in groups.items() if st]
        stale_dates = [
            r.publish_time
            for k, (rows, st) in groups.items() if st
            for r in rows if r.publish_time
        ]
        data_freshness = {
            "stale": bool(stale_cats),
            "stale_categories": stale_cats,
            # 陈旧数据"截至"的日期（这批里最新的那条）——如实描述数据有多旧
            "stale_data_date": (
                max(stale_dates).date().isoformat() if stale_dates else None
            ),
            "newest_news_date": newest.date().isoformat() if newest else None,
            # 补跑时"今天"应指截止时点那一天，而不是真正的今天
            "today": (as_of.date() if as_of else date.today()).isoformat(),
        }
        if stale_cats:
            # 快讯是 7×24 的，出现"近 1 天无新闻"通常意味着**采集故障**，
            # 按故障处理（ERROR）而非日常标记——否则会被噪音淹没
            logger.error(
                f"数据陈旧：{'、'.join(stale_cats)} 类目近 1 天无新闻，"
                f"使用截至 {data_freshness['stale_data_date']} 的数据。"
                "快讯为 7×24 供应，出现此情况通常意味着采集链路有问题。"
            )

        # 运行环境：交易日历降级（会按「工作日」猜测，节假日可能误判）
        env = {"calendar_degraded": _calendar_degraded()}
    finally:
        db.close()

    ctx = {
        "macro_news": _fmt_news(macro),
        "industry_news": _fmt_news(industry),
        "sector_summary": sector_summary,
        "capital_news": _fmt_news(capital),
        "comprehensive_news": _fmt_news(comprehensive),  # 广播给全部专家
        "all_news": _fmt_news(all_news),
        "market_snapshot": a_share,
        "us_market": us,
        "indicators": indicators,
    }
    stale_note = (
        f"；⚠️ 数据陈旧（最新新闻 {data_freshness['newest_news_date']}）"
        if data_freshness["stale"] else ""
    )
    logger.info(
        f"prepare 完成：专家输入已就绪；信息量="
        f"{'低' if info_level.get('low_info') else '正常'}"
        f"（{info_level.get('reason', '')}）{stale_note}"
    )
    return {"ctx": ctx, "info_level": info_level,
            "data_freshness": data_freshness, "env": env}


# ================= 专家节点 =================
def _expert_node(name: str, fn) -> callable:
    """构造一个专家节点（失败时降级跳过，不中断整个流程）。"""

    def node(state: AnalystState) -> dict:
        ctx = state.get("ctx", {})
        try:
            opinion = fn(get_llm(temperature=0.2, part="expert"), ctx)
            return {"opinions": [opinion.model_dump()]}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{name}] 分析失败，跳过：{e}")
            return {"failed": [name]}

    node.__name__ = f"{name}_node"
    return node


def risk_node(state: AnalystState) -> dict:
    """风险官：在分析师之后运行，能看到他们的结论并逐条反驳。"""
    ctx = dict(state.get("ctx", {}))
    opinions = state.get("opinions", [])
    ctx["other_opinions"] = "\n".join(
        f"【{o['expert']}】{o['stance']}（score={o['score']:+.2f}，置信度={o['confidence']}）："
        + "；".join(o.get("key_points", []))
        for o in opinions
    ) or "无（其他专家分析均失败）"
    try:
        risk = run_risk_officer(get_llm(temperature=0.3, part="expert"), ctx)
        return {"risk_opinion": risk.model_dump()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[风险官] 分析失败，跳过：{e}")
        return {"risk_opinion": {}, "failed": ["风险官"]}


# ================= 汇总（代码计算定档） =================
def aggregate_node(state: AnalystState) -> dict:
    opinions = state.get("opinions", [])
    risk = state.get("risk_opinion", {}) or {}

    # 分母固定为**全量权重**：缺席专家贡献 0，不再静默把它的权重摊给其他人。
    # 这样"专家失败"会让综合分自然收敛向中性，而不是悄悄改变方法论。
    full_w = sum(EXPERT_WEIGHTS.values())
    weighted = (
        sum(o["score"] * EXPERT_WEIGHTS.get(o["expert"], 0.0) for o in opinions) / full_w
        if full_w else 0.0
    )

    scores = [o["score"] for o in opinions]
    divergence = round(max(scores) - min(scores), 2) if len(scores) >= 2 else 0.0

    # —— 置信度定档 ——
    idx = 1  # 基准 medium
    if len(scores) >= 2 and divergence < settings.divergence_low:
        idx += 1                      # 高度一致 → 上调
    if divergence > settings.divergence_high:
        idx -= 1                      # 严重分歧 → 下调
    risk_veto = risk.get("risk_level") == "高"
    if risk_veto:
        idx -= 1                      # 风险官判高风险 → 下调
    failed = state.get("failed", [])
    if failed:
        idx -= 1                      # 有专家缺席 → 信息不完整，下调
    idx = max(0, min(2, idx))
    confidence = CONF_LADDER[idx]

    if len(scores) < 2:
        desc = "样本不足，无法评估分歧"
    elif divergence < settings.divergence_low:
        desc = "专家高度一致"
    elif divergence > settings.divergence_high:
        desc = "专家严重分歧"
    else:
        desc = "专家存在分歧"

    if weighted > 0.15:
        sentiment = "偏多"
    elif weighted < -0.15:
        sentiment = "偏空"
    else:
        sentiment = "中性"

    info_level = state.get("info_level", {}) or {}
    data_freshness = state.get("data_freshness", {}) or {}
    quant = {
        "weighted_score": round(weighted, 3),
        "divergence": divergence,
        "divergence_desc": desc,
        "final_confidence": confidence,
        "risk_veto": risk_veto,
        "sentiment": sentiment,
        "failed_experts": failed,
        "info_level": info_level,
        "data_freshness": data_freshness,
        "env": state.get("env", {}) or {},
    }
    logger.info(
        f"汇总：加权分={quant['weighted_score']:+.2f} 分歧度={divergence} "
        f"置信度={confidence} 定调={sentiment} 风险降档={risk_veto}"
        + (f" 缺席专家={failed}" if failed else "")
    )
    return {"quant": quant}


def chief_node(state: AnalystState) -> dict:
    """首席策略师：撰写叙述内容（数字已由 aggregate 定好）。"""
    ctx = state.get("ctx", {})
    quant = state.get("quant", {})
    opinions = [ExpertOpinion(**o) for o in state.get("opinions", [])]
    risk = RiskOpinion(**state["risk_opinion"]) if state.get("risk_opinion") else RiskOpinion()

    try:
        narrative = run_chief(get_llm(temperature=0.2, part="expert"), ctx, opinions, risk, quant)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[首席] 汇总失败，使用降级结构：{e}")
        narrative = {
            "market_summary": "（首席汇总失败）",
            "key_drivers": [], "sector_opportunities": [],
            "risks": list(risk.risks), "reference_news": [], "consensus_note": "",
        }

    return {"final": {
        **narrative, **quant,
        "expert_opinions": [o.model_dump() for o in opinions],
        "risk_opinion": risk.model_dump(),
    }}


# ================= 降级：单次生成 =================
def _fallback_report(db: Session, as_of: datetime | None = None) -> dict:
    """全部专家失败时的兜底：沿用单次 LLM 生成，保证每天都有报告。

    `as_of` 不为空时按该时点取数与标注日期（补跑用）。
    """
    logger.warning("触发降级：使用单次生成模式")
    a_share, _ = _market_snapshot(db, as_of)
    from app.agent.experts import extract_json

    q = db.query(News)
    if as_of is not None:
        q = q.filter(News.publish_time <= as_of)
    news_rows = q.order_by(News.publish_time.desc()).limit(20).all()
    news_text = _fmt_news(news_rows)
    llm = get_llm(temperature=0.2, part="expert")
    resp = llm.invoke(REPORT_PROMPT_TEMPLATE.format(
        date=(as_of.date() if as_of else datetime.now().date()),
        market_data=a_share, news_context=news_text,
    ))
    data = extract_json(resp.content) or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "market_summary": str(data.get("market_summary", "")),
        "sentiment": data.get("sentiment", "中性"),
        "confidence": data.get("confidence", "medium"),
        "score": float(data.get("score", 0.0) or 0.0),
        "key_drivers": data.get("key_drivers", []),
        "sector_opportunities": data.get("sector_opportunities", []),
        "risks": data.get("risks", []),
        "reference_news": data.get("reference_news", []),
        "expert_opinions": [],
        "divergence": None,
        "consensus_note": "（降级模式：未启用多专家分析）",
        "risk_veto": False,
        # 降级报告同样要带时效与环境信息——否则恰好在最不可信的一份上失去标注
        "data_freshness": {"stale": None, "note": "降级模式，未做数据时效检查"},
        "env": {"calendar_degraded": _calendar_degraded(), "fallback": True},
    }


# ================= 构建图 =================
def build_graph():
    g = StateGraph(AnalystState)
    g.add_node("prepare", prepare_node)
    g.add_node("macro", _expert_node("宏观", run_macro_expert))
    g.add_node("industry", _expert_node("行业", run_industry_expert))
    g.add_node("capital", _expert_node("资金面", run_capital_expert))
    g.add_node("technical", _expert_node("技术面", run_technical_expert))
    g.add_node("risk", risk_node)
    g.add_node("aggregate", aggregate_node)
    g.add_node("chief", chief_node)

    g.add_edge(START, "prepare")
    # 四路并行 fan-out
    for n in ("macro", "industry", "capital", "technical"):
        g.add_edge("prepare", n)
        g.add_edge(n, "risk")      # fan-in 到风险官
    g.add_edge("risk", "aggregate")
    g.add_edge("aggregate", "chief")
    g.add_edge("chief", END)
    return g.compile()


_APP = None


def get_app():
    global _APP
    if _APP is None:
        _APP = build_graph()
    return _APP


# ================= 对外入口 =================
def _report_time_on(day: datetime) -> datetime:
    """某天的「报告生成时点」——取 config 的 `REPORT_TIME`（默认 18:00）。

    补跑以这个时点为截止，与当天实时生成的口径一致，两份报告可直接对比。
    """
    from app.config import settings

    try:
        hh, mm = (int(x) for x in settings.report_time.split(":")[:2])
        # 构造也放进 try：越界值（如 "25:00" / "18:60"）同样抛 ValueError，
        # 放在外面会漏网
        return datetime(day.year, day.month, day.day, hh, mm)
    except (ValueError, AttributeError, TypeError):
        logger.warning(f"REPORT_TIME 配置不可用（{settings.report_time!r}），按 18:00 处理")
        return datetime(day.year, day.month, day.day, 18, 0)


def generate_daily_report(db: Session, date: datetime | None = None) -> MarketReport:
    """跑多专家流程并入库，返回 MarketReport。

    - `date` 为空（调度器 / 实时生成）→ 走「现在」，**行为与改动前完全一致**。
    - `date` 非空（补跑）→ 以该日 `REPORT_TIME` 为截止时点取**历史**数据，
      否则会把今天的新闻/行情塞进标着过去日期的报告，回测时等于用未来预测过去。
    """
    if date is None:
        date = datetime.now()
        as_of: datetime | None = None
    else:
        as_of = _report_time_on(date)
        if as_of > datetime.now():
            # 截止时点在未来（补跑当天、但还没到 REPORT_TIME）——退回"现在"。
            # 否则会把今天的数据全部过滤掉，生成一份空报告。
            logger.warning(f"截止时点 {as_of} 晚于当前时间，回退为当前时间")
            as_of = datetime.now()

    final: dict = {}
    try:
        result = get_app().invoke(
            {"as_of": as_of.isoformat()} if as_of is not None else {}
        )
        final = result.get("final", {}) or {}
        if not final.get("expert_opinions") and result.get("opinions"):
            final["expert_opinions"] = result["opinions"]
    except Exception as e:  # noqa: BLE001
        logger.error(f"多专家流程异常，降级：{e}")

    if not final.get("market_summary"):
        final = _fallback_report(db, as_of)

    opinions = final.get("expert_opinions", [])

    info_level = final.get("info_level") or {}
    data_freshness = final.get("data_freshness") or {}

    report = MarketReport(
        date=date,
        report_type="daily",
        title=str(final.get("market_summary", ""))[:200],
        content=json.dumps(final, ensure_ascii=False, indent=2),
        sentiment=final.get("sentiment", "中性"),
        confidence=final.get("confidence") or final.get("final_confidence"),
        score=final.get("score") if final.get("score") is not None else final.get("weighted_score"),
        expert_opinions=json.dumps(opinions, ensure_ascii=False) if opinions else None,
        divergence=final.get("divergence"),
        risk_veto=final.get("risk_veto"),
        low_info=info_level.get("low_info"),
        data_stale=data_freshness.get("stale"),
        model=get_llm_model_name(part="expert"),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def score_news_sentiment(db: Session, limit: int = 400) -> int:
    """对**近期**新闻批量情绪打分（-1~1），返回打分条数。

    两个要点（修复历史遗留）：
    1. 只打近 `sentiment_score_days` 天的新闻——老新闻不参与研判，无需打分；
    2. 打分失败**保持 NULL**（不再写 0.0），以便下次重试。
       早期实现把失败写成 0.0，导致失败与"真中性"无法区分且永不重试。
    """
    from app.agent.experts import extract_json
    from app.agent.prompts import SENTIMENT_BATCH_PROMPT

    since = datetime.now() - timedelta(days=settings.sentiment_score_days)
    rows = (
        db.query(News)
        .filter(News.sentiment.is_(None), News.publish_time >= since)
        .order_by(News.publish_time.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return 0
    llm = get_llm(temperature=0.0, part="news")
    count = 0
    for i in range(0, len(rows), 20):
        batch = rows[i:i + 20]
        news_list = "\n".join(
            f"{j + 1}. {n.title}｜{(n.content or '')[:150]}"
            for j, n in enumerate(batch)
        )
        def _score(prompt: str) -> list:
            d = extract_json(llm.invoke(prompt).content)
            if not isinstance(d, list):
                raise ValueError("情绪打分输出非数组")
            return d

        try:
            data = call_with_retry(
                _score, SENTIMENT_BATCH_PROMPT.format(news_list=news_list),
                retry_label="情绪打分",
                retry_times=llm_retry_times(),
            )
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                idx = int(entry.get("id", -1)) - 1
                if 0 <= idx < len(batch):
                    batch[idx].sentiment = float(entry.get("score", 0.0))
                    batch[idx].summary = entry.get("reason", "")
                    count += 1
        except Exception as e:  # noqa: BLE001
            # 不写 0.0：保持 NULL，下次可重试
            logger.warning(f"批量情绪打分失败（本批保持未打分）: {e}")
    db.commit()
    return count
