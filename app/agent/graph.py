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
import math
import operator
from datetime import date, datetime, time, timedelta
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from loguru import logger
from sqlalchemy.orm import Session

from app.agent.experts import (
    _as_list,
    run_capital_expert,
    run_chief,
    run_industry_expert,
    run_macro_expert,
    run_risk_officer,
    run_technical_expert,
)
from app.agent.indicators import compute_indicators, format_indicators
from app.agent.llm import get_llm, get_llm_model_name, with_output_parser
from app.agent.prompts import REPORT_PROMPT_TEMPLATE
from app.agent.schemas import ExpertOpinion, RiskOpinion
from app.config import settings
from app.db import SessionLocal
from app.models.market import MarketData
from app.models.news import News
from app.models.report import MarketReport
from app.services.news_selection import distinct_source_count, select_news

NEWS_PER_CATEGORY = settings.news_per_category
ALL_NEWS_LIMIT = settings.news_all_limit
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
    # 失败**是否可重试**（2026-10-22 新增）。`failed` 只记名字，
    # 而「整轮要不要重跑」取决于失败**原因**：网络断/限流/5xx/JSON 不合规
    # 重跑有意义，402 欠费/401/400 重跑纯白打（判据见 app.retry.is_retryable）。
    #
    # 元素是 **(专家名, 是否可重试) 二元组**，不是裸 bool —— 必须带名字：
    # 只存 bool 的话，风险官的「不可重试」会把四位分析师的「可重试」一票否决
    # （2026-10-22 复审发现）。而「要不要整轮重跑」的判据只看四位分析师，
    # 与 `_experts_all_gone` 的立场必须一致。
    retryable_failures: Annotated[list, operator.add]
    risk_opinion: dict
    quant: dict
    final: dict


# ================= 数据准备 =================
def _fmt_news(rows) -> str:
    if not rows:
        return "无"
    return "\n".join(f"- [{r.source}] {r.title}（{distinct_source_count(r)}源）" for r in rows)


def _fetch_news(db: Session, cats: list[str], limit: int,
                since: datetime, until: datetime | None = None
                ) -> tuple[list, bool]:
    """按类别取近期新闻。

    近 1 天无数据时回退到最新若干条（保证可跑），并返回 `stale=True`，
    以便下游**标注「数据陈旧」**——不再静默替换数据源。

    `until` 不为空时只取该时点**及之前**的新闻（补跑用）。
    **回退分支同样受 `until` 约束**——否则补跑时会捞到"未来"的新闻。
    """
    cutoff = until or datetime.now()

    def _query(with_since: bool):
        q = db.query(News).filter(News.category.in_(cats))
        if with_since:
            q = q.filter(News.publish_time >= since)
        q = q.filter(News.publish_time <= cutoff)
        if with_since:
            return select_news(q.all(), limit, since, cutoff)
        return q.order_by(News.publish_time.desc(), News.id).limit(limit).all()

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
    analyzed_at = as_of or datetime.now()
    db = SessionLocal()
    try:
        since = analyzed_at - timedelta(days=1)
        macro, st1 = _fetch_news(db, ["宏观", "政策"], NEWS_PER_CATEGORY, since, analyzed_at)
        industry, st2 = _fetch_news(db, ["行业", "公司"], NEWS_PER_CATEGORY, since, analyzed_at)
        capital, st3 = _fetch_news(db, ["资金", "市场"], NEWS_PER_CATEGORY, since, analyzed_at)
        comprehensive, st4 = _fetch_news(db, ["综合"], 10, since, analyzed_at)

        q = db.query(News).filter(News.publish_time >= since)
        q = q.filter(News.publish_time <= analyzed_at)
        all_news = select_news(q.all(), ALL_NEWS_LIMIT, since, analyzed_at)
        if not all_news:
            # 回退分支同样要受 as_of 约束，否则补跑会捞到"未来"的新闻
            q2 = db.query(News)
            q2 = q2.filter(News.publish_time <= analyzed_at)
            all_news = (
                q2.order_by(News.publish_time.desc()).limit(ALL_NEWS_LIMIT).all()
            )
        a_share, us = _market_snapshot(db, as_of)
        # 三项增量数据均加保护：任一失败只按缺省处理，不拖垮核心研判
        from app.services.info_level import (
            assess_info_level, classify_freshness, is_stale,
        )
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
        newest = max((n.publish_time for n in used + all_news if n.publish_time), default=None)

        # ---- H2（2026-09-23）：当日条数判据 ----
        # 原判据只看「近 1 天有没有新闻」→ 当天 0 条也判「新鲜」。
        # 详见 `classify_freshness` 的 docstring。
        _today = as_of.date() if as_of else date.today()
        # ⚠️ **按全表 count，不用 `used`** —— `used` 受每类目 limit 约束
        # （上限 8+8+8+10 = 34），当日新闻若集中在单一类目会被截断，
        # 于是「当天明明有 100 条」也会因为只看见 10 条而误报 warn。
        # 口径与 `assess_info_level` 的信号①同源（都是按日期的全表 count）。
        _q_today = db.query(News).filter(
            News.publish_time >= datetime.combine(_today, time.min),
            News.publish_time <= datetime.combine(_today, time.max),
        )
        _q_today = _q_today.filter(News.publish_time <= analyzed_at)
        today_count = _q_today.count()
        # ---- H1（2026-09-23）：采集轮是否还在跑 ----
        # 两者时间相近时，本轮日报**拿不到这一轮的新闻**（09-23 实测缺口 6 分 9 秒，
        # 日报据此写出「今日无重大消息」）。这里如实记录，让下游能区分
        # 「今天真没消息」与「今天的新闻还没落地」。
        from app.services.news_service import news_readiness
        # 状态表只存最近一次成功，不能重建历史分析时点的采集轮状态。
        ready = (news_readiness(db, now=analyzed_at) if as_of is None else {
            "last_round_at": None, "seconds_since": None,
            "collecting": None, "overdue": None,
        })
        from app.services.news_archive import coverage_counts
        coverage = (coverage_counts(db, since=analyzed_at - timedelta(days=1), until=analyzed_at)
                    if as_of is None else {"pending_count": None, "open_gap_count": None})
        collecting = ready["collecting"]
        freshness = classify_freshness(today_count, collecting=collecting)

        stale_cats = [k for k, (_, st) in groups.items() if st]
        stale_dates = [
            r.publish_time
            for k, (rows, st) in groups.items() if st
            for r in rows if r.publish_time
        ]
        data_freshness = {
            # H2（2026-09-23）：**当日 0 条也算陈旧**。原先只看「近 1 天无新闻」，
            # 于是 09-23 那种「当天 0 条、近 1 天有 1 条」被判为**新鲜** ——
            # 数据缺失在报告的 `data_stale` 字段上完全不可见，正是 H2 要修的。
            # 判定逻辑见 `is_stale`（抽出来是为了可测）。
            "stale": is_stale(bool(stale_cats), freshness),
            "stale_categories": stale_cats,
            # 陈旧数据"截至"的日期（这批里最新的那条）——如实描述数据有多旧
            "stale_data_date": (
                max(stale_dates).date().isoformat() if stale_dates else None
            ),
            "newest_news_date": newest.date().isoformat() if newest else None,
            "newest_news_at": newest.isoformat() if newest else None,
            "analyzed_at": analyzed_at.isoformat(),
            "readiness_scope": "historical_unknown" if as_of is not None else "snapshot",
            **coverage,
            "coverage_incomplete": bool(coverage["pending_count"] or coverage["open_gap_count"]),
            # 补跑时"今天"应指截止时点那一天，而不是真正的今天
            "today": (as_of.date() if as_of else date.today()).isoformat(),
            # ---- H2 / H1（2026-09-23 新增）----
            "today_count": today_count,
            "freshness": freshness,        # ok / warn / error
            "collecting": collecting,      # 采集轮是否仍在运行
            # ---- H1 就绪信号（2026-09-24 新增）----
            # 「上一轮**完整成功**落地」的时刻。日志里只写「今日新增=0」分不出
            # 「今天真没消息」与「新闻轮还没落地」，这一条给出可证伪的正向信息。
            "last_round_at": (
                ready["last_round_at"].isoformat() if ready["last_round_at"] else None
            ),
            "round_overdue": ready["overdue"],
        }
        if stale_cats:
            # 快讯是 7×24 的，出现"近 1 天无新闻"通常意味着**采集故障**，
            # 按故障处理（ERROR）而非日常标记——否则会被噪音淹没
            logger.error(
                f"数据陈旧：{'、'.join(stale_cats)} 类目近 1 天无新闻，"
                f"使用截至 {data_freshness['stale_data_date']} 的数据。"
                "快讯为 7×24 供应，出现此情况通常意味着采集链路有问题。"
            )
        if freshness == "error":
            logger.error(
                f"当日（{_today}）新闻 **0 条** —— 快讯是 7×24 的，"
                f"这不是「今天没消息」，而是**当日新闻没有进来**。"
                f"本轮报告据此写出的「今日无重大消息」等结论**不可信**。"
                + ("⚠️ 采集轮此刻仍在运行，本轮新闻尚未落地。"
                   if collecting else "请检查采集链路与 reports/app.log。")
            )
        elif collecting:
            logger.warning(
                "新闻采集轮仍在运行 —— 本轮研判**不包含**这一轮的新闻。"
                "若这是当日的主要新闻来源，报告的时效性会受影响。"
            )
        elif ready["overdue"]:
            # 采集轮**没在跑**，但上一轮成功已是超过一个采集间隔之前 ——
            # 说明本窗口那一轮没成（没跑 / 跑挂了 / 分类失败水位线没推进）。
            # 这与「今天真没消息」是完全不同的事实，必须显式说出来：
            # H1 的要害正在于它原本被静默吞掉，报告照写「今日无重大消息」。
            _secs = ready["seconds_since"]
            logger.warning(
                f"上一轮新闻采集距今 "
                + ("**从未成功过**" if _secs is None else f"{_secs / 60:.0f} 分钟")
                + f"（超过一个采集间隔 {settings.news_interval_minutes} 分钟）—— "
                f"**本窗口的新闻很可能没有落地**。"
                f"报告里「今日无重大消息」这类否定性结论不可信，"
                f"请先核对采集链路与 reports/app.log"
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
    # H1：ISO 时刻压到分钟（`2026-09-24T00:00:35.259` → `2026-09-24 00:00`），
    # 否则日志行会被秒级小数撑长、也读不出重点。
    _last_round = (data_freshness.get("last_round_at") or "")[:16].replace("T", " ")
    # 今日新增取不出来时给 '?'，**不要给 0** —— 0 会与「今天真的没有新消息」混淆，
    # 而这正是本次要修的那个误读。
    _signals = info_level.get("signals") or {}
    _new_count = (_signals.get("new_count") or {}).get("value", "?")
    logger.info(
        f"prepare 完成：专家输入已就绪；信息量="
        f"{'低' if info_level.get('low_info') else '正常'}"
        f"（{info_level.get('reason', '')}）{stale_note}"
        # 2026-09-23：**必须输出正向信息** —— 缺失信息无法证伪。
        # 那天的日报只写了「新增 0 条」，读者与专家 prompt 都分不出
        # 「今天真没消息」与「数据没进来」，于是把它解读成了前者。
        # 而「最新新闻=2026-09-22 22:29」这一条就足以让人一眼看出问题。
        f"｜最新新闻={newest or '无'}｜今日新增={_new_count}"
        f"｜新鲜度={data_freshness.get('freshness', '?')}"
        # H1（2026-09-24）：「上一轮成功落地」的时刻 —— 与「最新新闻」互补，
        # 前者说明**采集轮**跑没跑成，后者说明**库里**有什么。
        f"｜上轮采集={_last_round or '无'}"
        + ("｜**本窗口新闻未落地**" if data_freshness.get("round_overdue") else "")
        + ("｜**采集轮仍在运行，本轮新闻未纳入**"
           if data_freshness.get("collecting") else "")
    )
    return {"ctx": ctx, "info_level": info_level,
            "data_freshness": data_freshness, "env": env}


# ================= 专家节点 =================
def _expert_node(name: str, fn) -> callable:
    """构造一个专家节点（失败时降级跳过，不中断整个流程）。"""

    def node(state: AnalystState) -> dict:
        ctx = state.get("ctx", {})
        try:
            opinion = fn(get_llm(temperature=0.2, part="expert"), ctx, part="expert")
            return {"opinions": [opinion.model_dump()]}
        except Exception as e:  # noqa: BLE001
            # 记下「这条失败值不值得整轮重跑」。判据复用 app/retry.py，
            # **不在这里另写一套**（同一份事实只留一个副本）。
            from app.retry import is_retryable
            retryable = is_retryable(e)
            logger.warning(
                f"[{name}] 分析失败，跳过：{e}"
                + ("（可重试）" if retryable else "（不可重试，重跑无意义）")
            )
            return {"failed": [name], "retryable_failures": [(name, retryable)]}

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
        risk = run_risk_officer(get_llm(temperature=0.3, part="expert"), ctx,
                                part="expert")
        return {"risk_opinion": risk.model_dump()}
    except Exception as e:  # noqa: BLE001
        from app.retry import is_retryable
        retryable = is_retryable(e)
        logger.warning(
            f"[风险官] 分析失败，跳过：{e}"
            + ("（可重试）" if retryable else "（不可重试，重跑无意义）")
        )
        return {"risk_opinion": {}, "failed": ["风险官"],
                "retryable_failures": [("风险官", retryable)]}


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

    # 边界与 compute_backtest / _score_bucket **共用同一套 config**——
    # 各写各的会让同一份综合分被一处判「看空」、另一处判「中性」
    if weighted > settings.score_neutral_band:
        sentiment = "偏多"
    elif weighted < -settings.score_neutral_band:
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

    narrative: dict
    if not opinions:
        # 四位分析师**全部**缺席（2026-10-21 实测：DeepSeek 402 欠费那轮）。
        # 此时首席手里没有任何观点可汇总，再调一次 LLM 只会白打一次请求。
        #
        # ⚠️ 关键是 market_summary 必须是**空串**：下游 `generate_daily_report`
        # 靠 `if not market_summary` 判断要不要走报告级兜底（`_fallback_report`）。
        # 原实现在这里填的是「（首席汇总失败）」——**非空**，正好把兜底挡在门外，
        # 于是「该兜底」的情形成了唯一不兜底的情形。
        logger.warning(
            "[首席] 四位分析师全部缺席 —— 不再调用 LLM，交给报告级兜底（_fallback_report）"
        )
        narrative = {
            "title": "日报生成暂不可用",
            "market_summary": "",
            "key_drivers": [], "sector_opportunities": [],
            "risks": list(risk.risks), "reference_news": [], "consensus_note": "",
            "degraded": True,
        }
    else:
        try:
            narrative = run_chief(get_llm(temperature=0.2, part="expert"), ctx, opinions,
                                  risk, quant, part="expert")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[首席] 汇总失败，使用降级结构：{e}")
            narrative = {
                "title": "日报生成暂不可用",
                "market_summary": "（首席汇总失败）",
                "key_drivers": [], "sector_opportunities": [],
                "risks": list(risk.risks), "reference_news": [], "consensus_note": "",
                # 标记降级：便于日志/评估层区分「专家都在、只缺首席叙述」
                "degraded": True,
            }

    return {"final": {
        **narrative, **quant,
        "expert_opinions": [o.model_dump() for o in opinions],
        "risk_opinion": risk.model_dump(),
    }}


# ================= 降级：单次生成 =================
def _fallback_report(db: Session, as_of: datetime | None = None) -> dict:
    """全部专家失败时尝试单次 LLM 生成；失败记录由外层入口处理。

    `as_of` 不为空时按该时点取数与标注日期（补跑用）。
    """
    logger.warning("触发降级：使用单次生成模式")
    a_share, _ = _market_snapshot(db, as_of)
    from app.agent.experts import _require_dict

    cutoff = as_of or datetime.now()
    q = db.query(News).filter(News.publish_time <= cutoff)
    news_rows = q.order_by(News.publish_time.desc()).limit(20).all()
    news_text = _fmt_news(news_rows)
    llm = get_llm(temperature=0.2, part="expert")

    def parse(response):
        data = _require_dict(response.content, "降级报告")
        try:
            data["score"] = float(data.get("score", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("降级报告 score 非数值") from exc
        if not math.isfinite(data["score"]):
            raise ValueError("降级报告 score 非有限数值")
        return data

    parsed = with_output_parser(llm, parse)
    data = parsed.invoke(REPORT_PROMPT_TEMPLATE.format(
        date=cutoff.date(),
        market_data=a_share, news_context=news_text,
    ))
    return {
        "title": str(data.get("title", "")).strip(),
        "market_summary": str(data.get("market_summary", "")),
        "sentiment": data.get("sentiment", "中性"),
        # **兜底一律 low，不采信 LLM 自报的 confidence**（2026-10-23）。
        #
        # 立场与 `aggregate_node` 一致：那里有 `if failed: idx -= 1` ——
        # 「有专家缺席就降档」是项目既定的方法论。而兜底这条路**四位专家全都没参与**
        # （`expert_opinions` 恒为空），按同一条标准就该是最低档。
        #
        # 实测（2026-10-23 00:11 真实跑一次）它自报了 `high` —— 而依据只是
        # 「最新 20 条新闻 + 当日行情」的一次性生成，没有任何交叉验证；
        # 这种虚高会让下游把最不可信的一份当成最可信的。
        # 本函数的 data_freshness 注释写着「别恰好在最不可信的一份上失去标注」，
        # 置信度这行原先没跟上那个意图，属遗漏。
        "confidence": "low",
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


# ================= 重跑与降级的共用判据 =================
def _experts_all_gone(final: dict) -> bool:
    """四位分析师是否**全部**缺席。

    **只看 EXPERT_WEIGHTS 里的四位**，不含风险官：风险官是方向意见的补充，
    它单独缺席不该触发「整轮重跑」或「整份报告重写」（`aggregate_node` 已据此降档）。
    """
    failed = set(final.get("failed_experts") or [])
    return bool(failed) and all(name in failed for name in EXPERT_WEIGHTS)


# 这些异常说明「代码/数据本身有问题」，重跑必然复现 —— 不该吃 45s + 整轮。
# **注意不能把 ValueError 放进来**：LLM 输出不合规也抛 ValueError，而那种
# 换个模型往往就合规了，属于该重试的（`_FALLBACK_EXCEPTIONS` 里也这么定的）。
_DETERMINISTIC_EXC = (
    KeyError, TypeError, AttributeError, IndexError, ImportError,
    NotImplementedError, AssertionError, ZeroDivisionError,
)


def _is_deterministic(exc: BaseException) -> bool:
    """这个异常是不是**换个时刻重跑也必然复现**的。

    **为什么需要**（2026-10-22 复审发现 M-2）：`app/retry.py:is_retryable`
    的默认分支是 `return True`（除了 4xx 全算可重试），它的设计目标是
    「网络/超时/解析这类抖动值不值得再试一次」——对**单次调用**是合理的。
    但整轮重跑要花 45s + 1~3 分钟，用它当门槛太宽：`KeyError` / `TypeError` /
    `sqlite3.OperationalError` 这种 100% 必然复现的错误，每次都要多花一轮再
    失败一遍，还在日志里留下「可重试」的假信号。

    所以这里**只收紧「整轮重跑」这一处**，不改变 `is_retryable` 本身
    （它还被 app.retry 的重试装饰器与新闻采集拿来用，动它会波及别处）。
    """
    if isinstance(exc, _DETERMINISTIC_EXC):
        return True

    # 数据库错误（锁冲突 / 表不存在 / 列不存在）重跑没有意义。
    #
    # ⚠️ **必须同时看 SQLAlchemy 的包装**（2026-10-23 审查发现 M3）：
    # 项目经 SQLAlchemy 访问数据库，抛出的是 `sqlalchemy.exc.OperationalError`，
    # 它**不是** `sqlite3.Error` 的子类（实测 issubclass = False）。
    # 真正的 sqlite 异常藏在 `exc.orig` 里。只判 `sqlite3.Error` 会让
    # `database is locked` 漏网 → 白等 45s 再跑一整轮（最坏十几分钟）。
    import sqlite3

    if isinstance(exc, sqlite3.Error):
        return True
    try:
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(exc, SQLAlchemyError):
            return True
    except ImportError:  # pragma: no cover  # SQLAlchemy 是硬依赖，不会走到
        pass
    # 兜底：某些包装只把原始异常放在 `.orig` 上
    if isinstance(getattr(exc, "orig", None), sqlite3.Error):
        return True
    return False


# ---- 「摘要/标题是否为空或占位」的统一判据（2026-10-23 审查发现 M1/M3）----
#
# 为什么不直接用 `not x`：`not "   "` 是 False，纯空白串会同时绕过两档兜底；
# 而首席失败分支填的「（首席汇总失败）」虽是占位文案却非空。
# 这两种都会被当成"有综述"，导致空/占位内容原样落库。
_PLACEHOLDER_SUMMARIES = (
    "（首席汇总失败）",
    "(首席汇总失败)",
    "（首席未产出综述；以下为各专家意见与量化结果）",
)
_PLACEHOLDER_TITLES = (
    "日报生成暂不可用",
)


def _is_blank(v) -> bool:
    """None / 空串 / 纯空白（空格、换行、制表符）都算空。"""
    return not str(v or "").strip()


def _is_placeholder_summary(v) -> bool:
    """是不是「首席失败」留下的占位文案（非空，但不该当综述用）。"""
    return str(v or "").strip() in _PLACEHOLDER_SUMMARIES


def _is_placeholder_title(v) -> bool:
    """是不是「日报生成暂不可用」这类占位标题。"""
    return str(v or "").strip() in _PLACEHOLDER_TITLES


def _summary_from_experts(final: dict) -> str:
    """首席缺席时只复述已有结论，避免独立模型改写量化定调。"""
    count = len(final.get("expert_opinions") or [])
    parts = [f"依据已完成的 {count} 位专家分析，综合定调为{final.get('sentiment') or '未知'}"]
    score = final.get("weighted_score")
    if isinstance(score, (int, float)) and math.isfinite(score):
        parts[0] += f"（综合分 {score:+.3f}）"
    confidence = final.get("final_confidence") or final.get("confidence")
    if confidence:
        parts.append(f"置信度：{confidence}")
    if final.get("divergence_desc"):
        parts.append(str(final["divergence_desc"]))
    risk = final.get("risk_opinion") or {}
    if final.get("risk_veto"):
        parts.append("风险官已触发风险降档")
    risks = _as_list(risk.get("risks"))
    if risks:
        parts.append("风险提示：" + "；".join(risks))
    return "。".join(parts) + "。首席综述缺失，以上摘要由现有专家与量化结果生成。"


def _worth_retry(final: dict, retryable: bool) -> bool:
    """这一轮失败是否**值得整轮重跑**。

    **为什么不能只用 `_experts_all_gone`**（2026-10-22 自查发现）：
    `_run_experts` 在图**整体**抛异常时返回空 dict `{}`，此时 `failed_experts`
    也取不到 —— `_experts_all_gone({})` 是 `False`（"没人缺席"），重跑条件
    当场为假，于是「整个图挂掉」这种最该重跑的情形**反而一次都不重跑**。
    实测确认过：网络错误让图整体抛出时，`_run_experts` 只被调用 1 次。

    所以判据补上「`final` 为空 = 本轮没有产出」这一支：
        · 有产出但四位分析师全挂   → 可重跑（当轮什么观点都没拿到）
        · 压根没有产出（图异常）   → 可重跑（比上一种更彻底地失败）
    `retryable` 由 `app/retry.py:is_retryable` 判定，本函数不重新判断。
    """
    if not retryable:
        return False
    # `_graph_error` 是 `_run_experts` 在图整体抛异常时写回来的标记（2026-10-23）：
    # 那种情况下 final 不再是空 dict（要带上错误原因供事后追溯），
    # 所以这里不能只判 `not final` —— 必须显式认这个标记。
    if final.get("_graph_error"):
        return True
    return not final or _experts_all_gone(final)


def _run_experts(payload: dict) -> tuple[dict, bool]:
    """跑一次多专家流程，返回 `(final, 失败是否可重试)`。

    `retryable` 的含义：**这一轮的失败值不值得整轮重跑**。它由各节点在失败时
    用 `app/retry.py:is_retryable` 判定并累积到 state，这里只做汇总 ——
    判据仍然只有一处（`app/retry.py`），本函数不重新判断。
    """
    try:
        result = get_app().invoke(payload)
    except Exception as e:  # noqa: BLE001
        # ⚠️ **必须带堆栈**（2026-10-23 审查发现 M4）。
        # 原实现只有 `logger.error(f"...{e}")` —— 消息里有异常文本，但**没有
        # traceback**，无法定位是哪一行、哪个调用炸的。而这正是本条路径的
        # 唯一现场：下面的 `_diag` 只能从 `final` 取诊断，而 `final` 是空 dict。
        logger.opt(exception=True).error(f"多专家流程异常，降级：{e}")
        from app.retry import is_retryable
        # 图整体抛异常：按异常本身判断值不值得重跑。
        # 这里**没有** final，交给 `_worth_retry` 的 `not final` 分支处理。
        #
        # `_is_deterministic` 是整轮重跑这一处的收紧（M-2）：代码/数据库类错误
        # 重跑必然复现，不该白等 45s。
        # 把失败原因一起带出去 —— 否则落库后「为什么降级」完全查不出来：
        # `failed_experts` 在这条路径上恒为空（没有任何节点跑完）。
        _err_final = {
            "_graph_error": type(e).__name__,
            "_graph_error_msg": str(e)[:300],
        }
        if _is_deterministic(e):
            logger.warning(f"[研判] 该异常属确定性失败（{type(e).__name__}），不重跑")
            return _err_final, False
        return _err_final, is_retryable(e)

    # ---- 以下都不在 try 里 ----
    # ⚠️ 2026-10-22 复审发现（M-1）：把「记账」和「跑图」放同一个 try 是错的。
    # `retryable_failures` 的**形状**一变（比如裸 bool vs (name, bool)），
    # 解包就抛 TypeError → 被外层 except 吞掉 → 返回 ({}, True)，
    # **一份 market_summary 完好、四位专家都在的 final 就此蒸发**，
    # 接着白等 45s、整轮重跑、最后落到兜底。
    # 记账是锦上添花的数据，**不该有能力把主产物判死**。
    final = result.get("final", {}) or {}
    if not final.get("expert_opinions") and result.get("opinions"):
        final["expert_opinions"] = result["opinions"]

    # **只按四位分析师判断**：风险官的失败不参与这个决定。
    # （复审发现：原实现把风险官也算进来，风险官一次 402 就会把
    #   四位分析师的「可重试」一票否决掉，与 `_experts_all_gone` 矛盾。）
    #
    # 要求**全部**可重试才重跑：只要有一位是 402 这种确定性故障，
    # 重跑也必然在同一个专家上再次失败。
    expert_flags: list[bool] = []
    for item in (result.get("retryable_failures") or []):
        # 防御式解包：形状不对就跳过这条，绝不让它影响 final。
        if isinstance(item, (tuple, list)) and len(item) == 2:
            who, ok = item
            if who in EXPERT_WEIGHTS:
                expert_flags.append(bool(ok))
    retryable = bool(expert_flags) and all(expert_flags)
    return final, retryable

# ================= 对外入口 =================
def _report_time_on(day: datetime) -> datetime:
    """某天的「报告生成时点」——取 config 的 `REPORT_TIME`（默认 18:30）。

    补跑以这个时点为截止，与当天实时生成的口径一致，两份报告可直接对比。
    """
    from app.config import settings

    try:
        hh, mm = (int(x) for x in settings.report_time.split(":")[:2])
        # 构造也放进 try：越界值（如 "25:00" / "18:60"）同样抛 ValueError，
        # 放在外面会漏网
        return datetime(day.year, day.month, day.day, hh, mm)
    except (ValueError, AttributeError, TypeError):
        logger.warning(f"REPORT_TIME 配置不可用（{settings.report_time!r}），按 18:30 处理")
        return datetime(day.year, day.month, day.day, 18, 30)


def generate_daily_report(db: Session, date: datetime | None = None) -> MarketReport:
    """跑多专家流程并入库，返回 MarketReport。

    - `date` 为空（调度器 / 实时生成）→ 走「现在」，**行为与改动前完全一致**。
    - `date` 非空（补跑）→ 以该日 `REPORT_TIME` 为截止时点取**历史**数据，
      否则会把今天的新闻/行情塞进标着过去日期的报告，回测时等于用未来预测过去。
    - ⚠️ 若该截止时点**仍晚于当前时间**（补跑当天、但还没到 `REPORT_TIME`），
      则回退为**实时口径**（`as_of=None`）——避免把今天的数据全过滤掉生成空报告。
      此时**报告日期仍是传入的 `date`**，与内容口径不完全对应，日志里会说明。
    """
    if date is None:
        date = datetime.now()
        as_of: datetime | None = None
    else:
        as_of = _report_time_on(date)
        if as_of > datetime.now():
            # 截止时点在未来（补跑当天、但还没到 REPORT_TIME）——退回实时口径。
            # 否则会把今天的数据全部过滤掉，生成一份空报告。
            # 置回 None（而不是设成 now()）：这样 prepare_node 走的是**与实时生成
            # 完全相同的分支**——含信号①按入库统计——两份报告口径才可比。
            logger.warning(
                f"截止时点 {as_of} 晚于当前时间，回退为实时口径；"
                f"报告日期仍记为传入的 {date:%Y-%m-%d %H:%M}"
            )
            as_of = None

    payload = {"as_of": as_of.isoformat()} if as_of is not None else {}
    final, retryable = _run_experts(payload)
    # **保留"目前最好的一轮"**（2026-10-23 审查发现 S3）。
    #
    # 重跑是无条件覆盖 `final` 的：若第二轮图整体异常返回 `{}`，第一轮那份
    # 带着 `data_freshness` / `failed_experts` / `info_level` 的结果会被直接丢弃，
    # 随后兜底从空 dict 取诊断 → `failed_experts=[]`、`low_info=None`、
    # `data_stale=None`。**重跑反而让信息变少**，正好是本次要消灭的
    # 「静默降级、事后查不出」。所以每轮都比一比，只接受"更完整"的结果。
    best_final = final

    # 专家全员缺席 + 失败**全部可重试** → 整轮重跑（2026-10-22 新增）。
    #
    # **为什么按「可重试」分档**：本机网络会频繁短暂断网（用户环境，无服务器），
    # `Connection error` 是瞬时的，等一分钟重跑大概率就恢复；而 402 欠费 /
    # 401 鉴权 / 400 请求错是确定性故障 —— 重跑十遍一样，还白花几分钟。
    # 判据复用 `app/retry.py:is_retryable`，不另写一套。
    #
    # 重跑次数上限 = `report_retry_when_partial`（默认 1 = 额外跑一次）。
    max_retries = max(0, settings.report_retry_when_partial)
    for attempt in range(1, max_retries + 1):
        if not _worth_retry(final, retryable):
            break
        delay = max(0.0, settings.report_retry_delay_seconds)
        # 措辞要区分两种情况：图整体挂掉时并没有「专家缺席」这回事，
        # 说成「四位分析师全部缺席」会让看日志的人误判成 402 那类问题。
        _why = "本轮未产出任何报告（图整体异常）" if not final else "四位分析师全部缺席"
        logger.warning(
            f"[研判] {_why}且失败**可重试** —— "
            f"{delay:.0f}s 后整轮重跑（第 {attempt}/{max_retries} 次）"
        )
        if delay:
            # ⚠️ 这里不能写 `time.sleep`：本模块的 `time` 是 `datetime.time`，
            # 不是 time 模块（`from datetime import ... time ...`）。用局部导入避开歧义。
            from time import sleep as _sleep
            _sleep(delay)
        _new_final, retryable = _run_experts(payload)
        # 只有新一轮**确实产出了内容**才替换；空 dict 直接忽略，
        # 保留上一轮的诊断字段（S3）。
        if _new_final and not _new_final.get("_graph_error"):
            final = _new_final
            best_final = _new_final
        else:
            logger.warning(
                f"[研判] 重跑第 {attempt} 次**没有任何产出** —— "
                f"沿用第 {attempt} 轮之前那份结果（避免更完整的数据被空结果覆盖）"
            )
            final = best_final
        logger.info(
            f"[研判] 重跑第 {attempt} 次结果："
            + ("本轮仍未产出任何报告（图异常或全员缺席）" if not final
               else "仍全部缺席" if _experts_all_gone(final)
               else "已有专家产出，采用本次结果")
        )

    # 报告级兜底（单次生成）的触发条件。
    #
    # ⚠️ 2026-10-21 改：**只看 market_summary 是否为空不够**。
    # 原判断是 `if not final.get("market_summary")`，而「首席汇总失败」那条分支
    # 填的是非空占位文案「（首席汇总失败）」—— 于是**最该兜底的情形反而不兜底**：
    # 五位专家全 402 落空的那一轮，_fallback_report 一次都没跑，
    # 直接落库了一份 title="日报生成暂不可用" 的占位行（实测 2026-10-20）。
    #
    # 现在把「全员缺席」也纳入判据。**只看四位分析师**：风险官是方向意见的
    # 补充，它缺席不该单独触发整份报告重写（`aggregate_node` 已据此降档）。
    # 任一分析师存活 → 仍以多专家结果为准，保持原有行为不变。
    _failed = set(final.get("failed_experts") or [])
    _all_experts_gone = _experts_all_gone(final)
    # **有专家观点时，绝不能整份替换**（2026-10-22 修既有缺陷）。
    #
    # 原判据 `not market_summary` 只要首席的 summary 是空串就整份走兜底，
    # 哪怕四位分析师都成功。实测后果（四位都在、summary 空串）：
    #   sentiment  偏空 → 中性      ← 定调被改写
    #   score      -0.35 → 0.0      ← 方向分数被抹平
    #   divergence 0.8 → None       ← 分歧度丢失
    #   risk_veto  True → False     ← **风险否决被静默丢弃（最危险）**
    #   expert_opinions           → 全丢
    # 这些全都是**代码算出来的、可信的**结果，只为了补一个空字段就整批作废，
    # 属于典型的"为了补洞把墙拆了"。
    #
    # 正确做法分两档：
    #   · 有专家观点 → **只补 market_summary / title**，quant 与 opinions 原样保留
    #   · 没有专家观点（四位全挂 / 图异常）→ 才整份走单次生成兜底
    _has_opinions = bool(final.get("expert_opinions"))
    # **"空" 的判据必须是 strip 后为空**（2026-10-23 审查发现 M1）:
    # `not "   "` 是 **False** —— 纯空白串会同时绕过 tier A 与 tier B，
    # 空摘要原样落库且 `degraded=None`，比空串更难排查（它看着"有内容"）。
    # 实测：`"   "` / `"\n"` 两种输入 → 兜底调用 0 次、摘要原样落库。
    _summary_blank = _is_blank(final.get("market_summary"))
    # 首席失败分支会填「（首席汇总失败）」这类**非空占位文案**，
    # 它同样不该被当成"有综述"（2026-10-23 审查发现 M3）。
    _summary_is_placeholder = _is_placeholder_summary(final.get("market_summary"))
    if _has_opinions and not _all_experts_gone and (_summary_blank or _summary_is_placeholder):
        logger.warning(
            "首席未产出有效 market_summary（%s）—— 仅补摘要，保留 %d 位专家观点与量化结果"
            "（score=%s, risk_veto=%s）"
            % ("占位文案" if _summary_is_placeholder else "为空",
               len(final.get("expert_opinions") or []),
               final.get("weighted_score"), final.get("risk_veto"))
        )
        # 已有专家产物时不再发起独立研判，摘要必须忠实于原定调与风险约束。
        final["market_summary"] = _summary_from_experts(final)
        # 标题若是占位文案（如「日报生成暂不可用」）也一并替换 ——
        # 否则库里/前端看到的是"不可用"，实际是完整报告。
        if not str(final.get("title") or "").strip() or _is_placeholder_title(final.get("title")):
            final["title"] = "研判降级：首席综述缺失"
        # 标注降级，便于评估层/前端区分（quant 与 opinions 都还在）
        final["degraded"] = True
        final["degraded_reason"] = "chief_summary_missing"
    elif _summary_blank or _summary_is_placeholder or _all_experts_gone:
        # ⚠️ 兜底**必须先留档再替换**（2026-10-22 复审发现 M2）：
        # `_fallback_report` 返回的是全新 dict，`final` 被整体替换后，
        # `failed_experts` / `info_level` / `data_freshness` / `degraded`
        # **全都丢了** —— 落库后 low_info=None、data_stale=None、
        # content 里没有 failed_experts，等于"为什么降级"这件事无从追溯。
        # 而这恰恰是本次事故要修的那个毛病：静默降级、事后查不出来。
        _prev_title = str(final.get("title") or "").strip()
        _diag = {
            "failed_experts": sorted(_failed),
            "info_level": final.get("info_level") or {},
            "data_freshness": final.get("data_freshness") or {
                "stale": None, "note": "降级模式，未做数据时效检查",
            },
            "degraded": True,
            "degraded_reason": "single_shot_fallback",
        }
        # 图整体异常时 `final` 里带着根因（`_graph_error`），一并留档 ——
        # 否则落库后只看到"降级了"，查不出为什么。
        if final.get("_graph_error"):
            _diag["graph_error"] = final.get("_graph_error")
            _diag["graph_error_msg"] = final.get("_graph_error_msg")
        # **风险官的风险点必须保留**（2026-10-23 审查发现 M7）。
        # `_fallback_report` 的返回里没有 `risk_opinion`，整份替换会把它丢掉。
        # 而「四位分析师全挂、但风险官成功」是真实存在的路径 —— 那是一次
        # **成功的 LLM 产出**，且 EXPERTS_DESIGN 明确要求「风险提示必须完整保留」。
        _prev_risk = final.get("risk_opinion") or {}
        if _prev_risk:
            _diag["risk_opinion"] = _prev_risk
            _diag["risk_veto"] = bool(final.get("risk_veto")) or _prev_risk.get("risk_level") == "高"
        _where = "四位分析师全部缺席" if _all_experts_gone else "首席未产出 market_summary"
        logger.warning(
            f"{_where}（缺席：{sorted(_failed) or '无'}）—— 触发报告级兜底（单次生成）"
        )
        # ⚠️ **兜底自身也可能失败**（2026-10-23 审查发现 S2 同类）。
        # 它用的是同一个 LLM 配置 —— 402 欠费 / 备用也挂时必然一起失败。
        # 若放任异常抛出，当天**一行都不落库**，比原来那份占位行更糟：
        # `/reports/today` 会回退到前一天，前端完全看不出今天失败过。
        try:
            final = {**_fallback_report(db, as_of), **_diag}
        except Exception as e:  # noqa: BLE001
            logger.opt(exception=True).error(
                f"报告级兜底本身失败（{type(e).__name__}: {e}）—— "
                f"改落一份**最小可用**的占位报告，避免当天零行"
            )
            _why = ("四位分析师全部缺席" if _all_experts_gone
                    else "首席未产出综述")
            final = {
                **_diag,
                "title": f"研判失败：{_why}",
                "market_summary": (f"（生成失败：{type(e).__name__}）"
                                   "本期无可用研判内容，请检查 LLM 配置与账户余额。"),
                "sentiment": "中性",
                "confidence": "low",
                "score": 0.0,
                "key_drivers": [], "sector_opportunities": [],
                "risks": _as_list(_prev_risk.get("risks")),
                "reference_news": [], "expert_opinions": [],
                "divergence": None,
                "consensus_note": "（生成失败：兜底也未能产出内容）",
                "risk_veto": bool(_diag.get("risk_veto")),
                "env": {"calendar_degraded": _calendar_degraded(), "fallback": True,
                        "generation_failed": True},
                "degraded_reason": "fallback_failed",
            }
        # 单次生成成功也必须并入风险官的全部风险点；字符串不能拆成字符。
        final["risks"] = list(dict.fromkeys(
            _as_list(final.get("risks")) + _as_list(_prev_risk.get("risks"))
        ))
        if not final.get("title") or _is_placeholder_title(final.get("title")):
            # 兜底自己没写标题时才考虑沿用首席那个；但首席在**全员缺席**分支里
            # 写的正是「日报生成暂不可用」—— 直接沿用等于把本次修的这个毛病
            # 又请回来了（标题看似"不可用"，实际内容来自单次生成兜底）。
            # 所以只沿用**非占位**的标题。
            final["title"] = (_prev_title if _prev_title and not _is_placeholder_title(_prev_title)
                              else "研判降级：单次生成")

    opinions = final.get("expert_opinions", [])

    info_level = final.get("info_level") or {}
    data_freshness = final.get("data_freshness") or {}

    # H2（2026-09-25）：按 **(report_type, report_day)** 幂等写入 ——
    # 同一天已有日报就**原地覆盖**，不再无脑 `db.add`。
    # 改动前这里是无条件 add，而同日的判定落在 `date`（含微秒的生成时刻）上，
    # 于是同一天能落多份（实测 09-15 落了 3 份），只不过**没有任何报错**：
    # `/reports/today` 只显示最后一份，回测得自己按天去重否则同一天被重复加权。
    from app.services.report_service import upsert_daily_report  # 循环导入：延后到调用时

    report_title = str(final.get("title") or "").strip()
    report_title = report_title.strip("\"'“”‘’").replace("\n", " ")[:24]
    if not report_title:
        # 兼容旧模型/旧降级响应：仅在 LLM 没有返回 title 时才回退，
        # 不再把完整综述直接截成标题。
        report_title = str(final.get("market_summary") or "日报研判").strip()
        report_title = report_title.split("。", 1)[0][:24] or "日报研判"

    report = upsert_daily_report(
        db,
        date.date(),                      # 业务日：`date` 传进来时就是报告日（补跑也如此）
        date=date,                        # 生成时刻，保留可追溯性
        title=report_title,
        content=json.dumps(final, ensure_ascii=False, indent=2),
        sentiment=final.get("sentiment", "中性"),
        confidence=final.get("confidence") or final.get("final_confidence"),
        score=final.get("score") if final.get("score") is not None else final.get("weighted_score"),
        expert_opinions=json.dumps(opinions, ensure_ascii=False) if opinions else None,
        divergence=final.get("divergence"),
        risk_veto=final.get("risk_veto"),
        low_info=info_level.get("low_info"),
        # 含义见 `is_stale`：近 1 天某类目无新闻 **或** 当日 0 条。
        data_stale=data_freshness.get("stale"),
        model=get_llm_model_name(part="expert"),
    )
    return report
