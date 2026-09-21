"""评估层：让风险官与首席策略师的产出可测量、可证伪。

阶段 S —— 瞬时检验（不依赖历史数据）
    S1 敏感性/空转  S2 复现性  S3 反驳具体性  S4 首席忠实度
    S5 内部一致性   S6 套话检测  S7 输出多样性
阶段 A —— 纵轴检验（需时间序列，当前 n=3 无统计意义）
    A1 引用可追溯  A2 数字一致性  A3 风险等级分布
阶段 Q —— 数据质量检查
    Q1 类别覆盖与归一化漏网
阶段 B —— 骨架，等数据量
    B1 风险官校准  B2 过度谨慎
阶段 C —— 未实现，仅 TODO
    归因一致性

约束：所有检验均为只读，不写回 report / news / market_data。
"""
from __future__ import annotations

import difflib
import functools
import json
import re
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.models.market import MarketData
from app.models.news import News
from app.models.report import MarketReport

SH_SYMBOL = "sh000001"
STALE_DAYS = 7          # A1：判定"超窗引用"的天数
GROUNDING_THRESHOLD = 0.5   # S6：单条风险的落地阈值
EXPERT_NAMES = ["宏观", "行业", "资金面", "技术面"]
BULL_WORDS = ["上涨", "反弹", "利好", "机会", "走强", "回暖", "乐观", "企稳", "向好"]
BEAR_WORDS = ["下跌", "回调", "风险", "承压", "走弱", "悲观", "下行", "抛售", "谨慎"]


def _safe(check_name: str):
    """装饰器：捕获一切异常，返回 status=error，绝不向外抛出。"""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{check_name}] 检查失败: {e}")
                return {"check": check_name, "status": "error",
                        "reason": f"{type(e).__name__}: {e}"}
        return wrapper
    return deco


# ================= 公共读取工具 =================
def _load_reports(db: Session, limit: int = 90) -> list[dict]:
    """读取**日报**并解析 content / expert_opinions。

    周报不参与评估：其 content 是周区间聚合值（如"周涨跌幅-4.42%"），
    与单日口径不可比，混入会推高 A2「编造数字」的假阳性率，也会打乱 S7 的相似度序列。
    """
    rows = (
        db.query(MarketReport)
        .filter((MarketReport.report_type == "daily")
                | (MarketReport.report_type.is_(None)))
        .order_by(MarketReport.date.desc())
        .limit(limit)
        .all()
    )
    out = []
    for r in rows:
        try:
            content = json.loads(r.content) if r.content else {}
        except (json.JSONDecodeError, TypeError):
            content = {}
        if not isinstance(content, dict):
            content = {}
        try:
            experts = json.loads(r.expert_opinions) if r.expert_opinions else []
        except (json.JSONDecodeError, TypeError):
            experts = []
        out.append({
            "date": r.date,
            "sentiment": r.sentiment,
            "confidence": r.confidence,
            "score": r.score,
            "divergence": r.divergence,
            "risk_veto": r.risk_veto,
            "expert_opinions": experts if isinstance(experts, list) else [],
            "content": content,
        })
    return out


def _latest_market_map(db: Session) -> dict:
    """每个 symbol 的最新一条行情。"""
    rows = db.query(MarketData).order_by(MarketData.date.desc()).all()
    latest: dict = {}
    for r in rows:
        latest.setdefault(r.symbol, r)
    return latest


def _next_day_stats(db: Session, date: datetime) -> dict | None:
    """某日之后（不含当日）最近一个交易日的行情统计。"""
    rows = (
        db.query(MarketData).filter(MarketData.symbol == SH_SYMBOL)
        .order_by(MarketData.date.asc()).all()
    )
    for i, r in enumerate(rows):
        if r.date.date() > date.date():
            prev_close = rows[i - 1].close if i > 0 else None
            amp = None
            if prev_close and r.high and r.low:
                amp = (r.high - r.low) / prev_close
            return {"change_pct": r.change_pct, "amplitude": amp}
    return None


# =====================================================================
# 阶段 S —— 瞬时检验
# =====================================================================
def _bullish_ctx() -> dict:
    good = ("央行宣布降准0.5个百分点，释放长期资金1万亿元（新浪财经）\n"
            "证监会出台活跃资本市场新规，鼓励中长期资金入市（财联社）")
    ind_good = ("多地延续新能源车补贴政策，产业链订单饱满（东方财富）\n"
                "半导体龙头宣布扩产，行业景气度上行（财联社）")
    return {
        "macro_news": good,
        "industry_news": ind_good,
        "capital_news": "北向资金单日净流入超200亿元，创年内新高（新浪财经）",
        "comprehensive_news": "无",
        "all_news": good + "\n" + ind_good,
        "market_snapshot": "上证指数(sh000001): 收盘 3200.0，涨跌幅 2.50%（2026-09-14）",
        "us_market": "标普500(.INX): 收盘 5800.0，涨跌幅 1.80%",
        "indicators": ("最新收盘：3200.0\nMA5=3100.0　MA20=3050.0　MA60=3000.0\n"
                       "RSI14=68.0\n5日动量=3.2%　20日动量=5.1%\n量比=1.4\n"
                       "形态：多头排列（价格站上全部均线：MA5、MA20、MA60）"),
    }


def _bearish_ctx() -> dict:
    bad = ("美联储意外加息25bp，全球风险资产承压（新浪财经）\n"
           "证监会立案调查多家上市公司信息披露违规（财联社）")
    ind_bad = ("工信部收紧光伏产能审批，行业需求走弱（东方财富）\n"
               "半导体库存高企，龙头下调全年指引（财联社）")
    return {
        "macro_news": bad,
        "industry_news": ind_bad,
        "capital_news": "北向资金单日净流出超180亿元，创年内新高（新浪财经）",
        "comprehensive_news": "无",
        "all_news": bad + "\n" + ind_bad,
        "market_snapshot": "上证指数(sh000001): 收盘 3000.0，涨跌幅 -2.50%（2026-09-14）",
        "us_market": "标普500(.INX): 收盘 5400.0，涨跌幅 -2.10%",
        "indicators": ("最新收盘：3000.0\nMA5=3150.0　MA20=3200.0　MA60=3250.0\n"
                       "RSI14=28.0\n5日动量=-4.1%　20日动量=-6.3%\n量比=1.6\n"
                       "形态：空头排列（价格跌破全部均线：MA5、MA20、MA60）"),
    }


def _empty_ctx() -> dict:
    return {
        "macro_news": "无", "industry_news": "无", "capital_news": "无",
        "comprehensive_news": "无", "all_news": "无",
        "market_snapshot": "无", "us_market": "无",
        "indicators": "技术指标不可用：行情数据不足",
    }


def _expert_func(name: str):
    from app.agent.experts import (
        run_capital_expert, run_industry_expert,
        run_macro_expert, run_technical_expert,
    )
    return {
        "宏观": run_macro_expert, "行业": run_industry_expert,
        "资金面": run_capital_expert, "技术面": run_technical_expert,
    }[name]


@_safe("sensitivity")
def check_sensitivity(db: Session | None = None, expert: str = "行业") -> dict:
    """S1 敏感性/空转检测：喂极端合成输入，看是否朝对应方向响应。

    三个场景各自过/不过（绝对阈值），**外加一条看多/看空的对称性检查**——
    绝对阈值只能测"是否响应"，测不出"刻度是否偏向一边"（`EVAL_SPEC.md` §附注
    本就承认了这一点）。看多与看空的合成场景是**对称构造的**（同为 ±2.50%、
    ±180~200 亿北向、多头/空头排列），所以两边评分绝对值理应相当。

    不读写数据库——ctx 为手工构造。
    """
    from app.agent.llm import get_llm

    fn = _expert_func(expert)
    llm = get_llm(temperature=0.2, part="expert")
    scenarios, passed = [], 0
    for name, ctx, ok in [
        ("bullish", _bullish_ctx(), lambda s: s > 0.3),
        ("bearish", _bearish_ctx(), lambda s: s < -0.3),
        ("empty", _empty_ctx(), lambda s: abs(s) <= 0.2),
    ]:
        try:
            op = fn(llm, ctx)
            good = bool(ok(op.score))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[S1] 场景 {name} 调用失败: {e}")
            op, good = None, False
        scenarios.append({
            "name": name,
            "score": round(op.score, 3) if op else None,
            "stance": op.stance if op else None,
            "pass": good,
        })
        passed += int(good)

    result = {"check": "sensitivity", "status": "ok", "expert": expert,
              "scenarios": scenarios, "passed": passed, "total": len(scenarios)}

    # 对称性检查：看多与看空的评分绝对值应当相当。
    #
    # 为什么单独查这一条：绝对阈值（>0.3）若恰好落在模型输出的正中间，
    # 检查会时过时不过；而「刻度偏向一边」这种问题，**只有对比两个方向才看得出**。
    # 依据：LLM 的极性偏差是已知现象，且方向因模型而异（文献建议评估须分极性看），
    # 所以需要一条显式的对称性约束兜住。
    # 参考区间取 1.67 倍：真实市场的波动率不对称通常在 1.2~1.5 倍量级。
    SYM_MIN, SYM_MAX = 0.6, 1.67
    symmetry = None
    bull, bear = scenarios[0]["score"], scenarios[1]["score"]
    if bull is not None and bear is not None and bear != 0:
        ratio = abs(bull) / abs(bear)
        ok_sym = SYM_MIN <= ratio <= SYM_MAX
        symmetry = {"ratio": round(ratio, 2), "in_range": ok_sym,
                    "range": [SYM_MIN, SYM_MAX]}

    flags = []
    # 「空转」的判据是**方向**，不是幅度：分值朝对的方向动了就算响应了数据。
    # 幅度不够是**刻度问题**，由下面的对称性检查负责——
    # 若沿用 pass（含 >0.3 的幅度阈值）来判空转，会在"看多 0.30"这种
    # "确实响应了、只是偏低"的情况下误报"空转"，与对称性结论自相矛盾。
    bull_s, bear_s = scenarios[0]["score"], scenarios[1]["score"]
    if (bull_s is not None and bull_s <= 0) or (bear_s is not None and bear_s >= 0):
        flags.append("agent 可能未在响应数据（空转）")
    if not scenarios[2]["pass"]:
        flags.append("数据缺失时仍在编造结论")
    if symmetry and not symmetry["in_range"]:
        side = "看多" if symmetry["ratio"] < SYM_MIN else "看空"
        flags.append(
            f"打分不对称：|看多|/|看空|={symmetry['ratio']}，超出 "
            f"{SYM_MIN}~{SYM_MAX}，{side}方向评分偏低（锚点刻度可能未对齐）"
        )
    result["symmetry"] = symmetry
    if flags:
        result["flag"] = "；".join(flags)
    return result


@_safe("reproducibility")
def check_reproducibility(db: Session, runs: int = 3, expert: str = "行业") -> dict:
    """S2 复现性：同一输入跑 N 次，测结论稳定性。"""
    from app.agent.graph import prepare_node
    from app.agent.llm import get_llm

    ctx = prepare_node({}).get("ctx", {})
    fn = _expert_func(expert)
    llm = get_llm(temperature=0.2, part="expert")

    scores, stances = [], []
    for _ in range(max(2, runs)):
        try:
            op = fn(llm, ctx)
            scores.append(round(op.score, 3))
            stances.append(op.stance)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[S2] 调用失败: {e}")

    if len(scores) < 2:
        return {"check": "reproducibility", "status": "error",
                "reason": "有效调用不足 2 次"}

    mean = sum(scores) / len(scores)
    std = (sum((s - mean) ** 2 for s in scores) / len(scores)) ** 0.5
    rng = max(scores) - min(scores)
    flips = len(set(stances)) - 1

    result = {
        "check": "reproducibility", "status": "ok", "expert": expert,
        "runs": len(scores), "scores": scores,
        "std": round(std, 3), "range": round(rng, 3),
        "stances": stances, "stance_flips": flips,
        "stable": not (std > 0.3 or flips > 0),
    }
    if not result["stable"]:
        result["flag"] = "输出不稳定，下游统计结论不可靠"
    return result


@_safe("risk_specificity")
def check_risk_specificity(db: Session, limit: int = 90) -> dict:
    """S3 风险官反驳的「具体性」：反驳是否点名了某位专家。"""
    reports = _load_reports(db, limit)
    total, specific, skipped = 0, 0, 0
    for r in reports:
        args = (r["content"].get("risk_opinion") or {}).get("counter_arguments") or []
        if not args:
            skipped += 1
            continue
        for a in args:
            total += 1
            if any(name in str(a) for name in EXPERT_NAMES):
                specific += 1

    if total == 0:
        return {"check": "risk_specificity", "status": "skipped",
                "reason": "无报告包含 risk_opinion.counter_arguments（可能为旧格式报告）",
                "reports_scanned": len(reports)}

    rate = round(specific / total, 3)
    result = {"check": "risk_specificity", "status": "ok",
              "reports_scanned": len(reports), "total_arguments": total,
              "specific": specific, "specificity_rate": rate}
    if rate < 0.5:
        result["flag"] = "反驳泛化，未针对具体观点（退化为风险朗读机）"
    return result


@_safe("chief_faithfulness")
def check_chief_faithfulness(db: Session, limit: int = 90) -> dict:
    """S4 首席忠实度：是否完整并入风险官意见 / 是否披露严重分歧。"""
    reports = _load_reports(db, limit)
    checked, retained = 0, 0
    div_cases, div_disclosed = 0, 0

    for r in reports:
        risk = r["content"].get("risk_opinion") or {}
        risk_points = [str(x) for x in (risk.get("risks") or [])]
        if risk_points:
            checked += 1
            risks_field = " ".join(str(x) for x in (r["content"].get("risks") or []))
            if all(any(p[:15] in risks_field for p in [rp]) for rp in risk_points):
                retained += 1

        div = r.get("divergence")
        if div is not None and div > settings.divergence_high:
            div_cases += 1
            text = (str(r["content"].get("consensus_note", ""))
                    + str(r["content"].get("market_summary", "")))
            if any(k in text for k in ("分歧", "不一致", "矛盾", "争议")):
                div_disclosed += 1

    if checked == 0:
        return {"check": "chief_faithfulness", "status": "skipped",
                "reason": "无报告包含 risk_opinion.risks（可能为旧格式报告）",
                "reports_scanned": len(reports)}

    retention = round(retained / checked, 3)
    result = {
        "check": "chief_faithfulness", "status": "ok",
        "reports_scanned": len(reports), "risk_retention": retention,
        "divergence_cases": div_cases,
        "divergence_disclosed": (
            round(div_disclosed / div_cases, 3) if div_cases else None
        ),
    }
    flags = []
    if retention < 1.0:
        flags.append("风险官意见未被完整并入（护栏失效）")
    if div_cases and result["divergence_disclosed"] < 1.0:
        flags.append("首席淡化了专家分歧")
    if flags:
        result["flag"] = "；".join(flags)
    return result


@_safe("internal_consistency")
def check_internal_consistency(db: Session, limit: int = 90) -> dict:
    """S5 内部一致性：市场综述措辞倾向 vs 代码定调。"""
    reports = _load_reports(db, limit)
    checked, consistent, bad = 0, 0, []

    for r in reports:
        sentiment = r.get("sentiment")
        summary = str(r["content"].get("market_summary", "") or "")
        if not sentiment or not summary:
            continue
        checked += 1
        tone = sum(w in summary for w in BULL_WORDS) - sum(w in summary for w in BEAR_WORDS)
        tone_dir = 1 if tone > 0 else (-1 if tone < 0 else 0)
        sent_dir = 1 if sentiment == "偏多" else (-1 if sentiment == "偏空" else 0)
        # 仅在方向明确相反时判为不一致（保守，避免误报）
        ok = not (tone_dir != 0 and sent_dir != 0 and tone_dir != sent_dir)
        consistent += int(ok)
        if not ok:
            bad.append({"date": str(r["date"].date()), "sentiment": sentiment,
                        "tone": "偏多" if tone_dir > 0 else "偏空"})

    if checked == 0:
        return {"check": "internal_consistency", "status": "skipped",
                "reason": "无可用报告（缺 sentiment 或 market_summary）"}

    rate = round(consistent / checked, 3)
    result = {"check": "internal_consistency", "status": "ok",
              "reports_scanned": len(reports), "checked": checked,
              "consistent": consistent, "consistency_rate": rate,
              "inconsistent": bad}
    if rate < 0.7:
        result["flag"] = "市场综述措辞与定调不一致"
    return result


@_safe("risk_grounding")
def check_risk_grounding(db: Session, limit: int = 90,
                         threshold: float = GROUNDING_THRESHOLD) -> dict:
    """S6 风险官套话检测：风险点是否与当天新闻/专家观点相关。"""
    from app.rag.embeddings import embed_documents

    reports = _load_reports(db, limit)
    total, grounded, ungrounded = 0, 0, []

    for r in reports:
        risks = [str(x) for x in ((r["content"].get("risk_opinion") or {}).get("risks") or [])]
        if not risks:
            continue

        # 参考语料 = 日期窗口内的新闻 + 本报告的专家观点文本
        since = r["date"] - timedelta(days=STALE_DAYS)
        news_rows = (
            db.query(News)
            .filter(News.publish_time >= since, News.publish_time <= r["date"])
            .limit(120).all()
        )
        corpus = [f"{n.title} {n.content or ''}"[:300] for n in news_rows]
        corpus += [
            " ".join(str(p) for p in (o.get("key_points") or []))
            for o in r["expert_opinions"]
        ]
        corpus = [c for c in corpus if c.strip()]
        if not corpus:
            continue

        texts = risks + corpus
        vecs = embed_documents(texts)
        risk_vecs, corpus_vecs = vecs[:len(risks)], vecs[len(risks):]

        for risk, rv in zip(risks, risk_vecs):
            sims = [_cosine(rv, cv) for cv in corpus_vecs]
            best = max(sims) if sims else 0.0
            total += 1
            if best >= threshold:
                grounded += 1
            else:
                ungrounded.append({"date": str(r["date"].date()),
                                   "risk": risk[:80], "max_sim": round(best, 3)})

    if total == 0:
        return {"check": "risk_grounding", "status": "skipped",
                "reason": "无报告包含 risk_opinion.risks，或无可用参考语料"}

    rate = round(grounded / total, 3)
    result = {"check": "risk_grounding", "status": "ok",
              "reports_scanned": len(reports), "total_risks": total,
              "grounded": grounded, "grounding_rate": rate,
              "ungrounded": ungrounded[:10]}
    if rate < 0.6:
        result["flag"] = "风险点与当天新闻关联弱，疑似套话"
    return result


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


@_safe("output_diversity")
def check_output_diversity(db: Session, limit: int = 90) -> dict:
    """S7 输出多样性：连续报告是否高度雷同（模板化）。"""
    reports = _load_reports(db, limit)
    reports = [r for r in reports if r["content"].get("market_summary")]
    if len(reports) < 3:
        return {"check": "output_diversity", "status": "insufficient_data",
                "reason": f"仅 {len(reports)} 份含 market_summary 的报告，构不成对比对（需 ≥3）",
                "reports_scanned": len(reports)}

    # 按日期升序，比较相邻报告
    ordered = sorted(reports, key=lambda x: x["date"])
    sims, overlaps = [], []
    for a, b in zip(ordered, ordered[1:]):
        sa = str(a["content"].get("market_summary", ""))
        sb = str(b["content"].get("market_summary", ""))
        sims.append(difflib.SequenceMatcher(None, sa, sb).ratio())
        da = {str(x) for x in (a["content"].get("key_drivers") or [])}
        db_ = {str(x) for x in (b["content"].get("key_drivers") or [])}
        if da or db_:
            overlaps.append(len(da & db_) / max(1, len(da | db_)))

    # 每份报告内专家立场的多样性
    diversities = []
    for r in reports:
        stances = [o.get("stance") for o in r["expert_opinions"] if o.get("stance")]
        if stances:
            diversities.append(len(set(stances)) / len(stances))

    summary_sim = round(sum(sims) / len(sims), 3) if sims else None
    result = {
        "check": "output_diversity", "status": "ok",
        "reports_scanned": len(reports),
        "summary_similarity": summary_sim,
        "drivers_overlap": round(sum(overlaps) / len(overlaps), 3) if overlaps else None,
        "stance_diversity_avg": (
            round(sum(diversities) / len(diversities), 3) if diversities else None
        ),
    }
    flags = []
    if summary_sim is not None and summary_sim > 0.7:
        flags.append("市场综述高度雷同，疑似模板化输出")
    if (len(reports) >= 10 and result["stance_diversity_avg"] is not None
            and result["stance_diversity_avg"] < 0.3):
        flags.append("专家长期同向，可能未独立分析")
    if flags:
        result["flag"] = "；".join(flags)
    return result


# =====================================================================
# 阶段 A —— 纵轴检验
# =====================================================================
def _normalize_title(s: str) -> str:
    """归一化标题：去括号及内容、去标点空白。"""
    s = str(s or "")
    for lb, rb in [("【", "】"), ("[", "]"), ("（", "）"), ("(", ")"), ("《", "》")]:
        s = re.sub(re.escape(lb) + "[^" + re.escape(rb) + "]*" + re.escape(rb), "", s)
    return re.sub(r"[\s\W_]+", "", s)


@_safe("reference_traceability")
def check_reference_traceability(db: Session, limit: int = 90) -> dict:
    """A1 引用可追溯：检测首席是否编造新闻引用。"""
    reports = _load_reports(db, limit)
    news_rows = db.query(News).all()
    corpus = [(_normalize_title(n.title), n.title, n.publish_time) for n in news_rows]
    corpus = [(norm, raw, pt) for norm, raw, pt in corpus if norm]

    total, traceable, stale = 0, 0, []
    untraceable = []
    scanned = 0

    for r in reports:
        refs = r["content"].get("reference_news")
        if not isinstance(refs, list) or not refs:
            continue
        scanned += 1
        for t in refs:
            t = str(t)
            if not t.strip():
                continue
            total += 1
            norm_t = _normalize_title(t)
            hit = None
            for norm_n, raw, pt in corpus:
                if not norm_n or not norm_t:
                    continue
                if norm_n.find(norm_t[:12]) >= 0 or norm_t.find(norm_n[:12]) >= 0:
                    hit = (raw, pt)
                    break
                if difflib.SequenceMatcher(None, norm_n, norm_t).ratio() >= 0.6:
                    hit = (raw, pt)
                    break
            if hit:
                traceable += 1
                if hit[1] and hit[1] < r["date"] - timedelta(days=STALE_DAYS):
                    stale.append({"date": str(r["date"].date()), "title": t[:60],
                                  "news_date": str(hit[1].date())})
            else:
                untraceable.append({"date": str(r["date"].date()), "title": t[:60]})

    if total == 0:
        return {"check": "reference_traceability", "status": "skipped",
                "reason": "无报告包含非空 reference_news 数组"}

    rate = round(traceable / total, 3)
    result = {
        "check": "reference_traceability", "status": "ok",
        "reports_scanned": scanned, "total_refs": total,
        "traceable": traceable, "hit_rate": rate,
        "stale_refs": stale[:10], "untraceable": untraceable[:10],
    }
    if rate < 0.8:
        result["flag"] = "疑似幻觉引用"
    return result


PCT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
POINT_RE = re.compile(r"(\d{3,5}(?:\.\d+)?)\s*点")
YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*年")
ORD_RE = re.compile(r"第\s*\d+")
HEDGE = ("约", "近", "左右", "超过", "逾", "超")
# 中文财报表述常用"涨/跌"字表达方向，而非正负号
UP_WORDS = ("涨", "升", "上行", "走高")
DOWN_WORDS = ("跌", "降", "回落", "下行", "走低")
SUBJECT_MAP = [
    ("上证", "sh000001"), ("深证", "sz399001"), ("成指", "sz399001"),
    ("创业板", "sz399006"), ("沪深300", "sh000300"),
    ("标普", ".INX"), ("纳斯达克", ".IXIC"), ("道琼斯", ".DJI"),
]


def _resolve_subject(summary: str, pos: int) -> str | None:
    """解析数字前的最近主语：按子句切分，取子句内**位置最靠后**的标的词。

    避免"沪深300跌0.84%，创业板指跌0.49"中，0.84 被错配到创业板。
    """
    clause = re.split(r"[，。；,;.]", summary[:pos])[-1]
    best_sym, best_pos = None, -1
    for key, sym in SUBJECT_MAP:
        p = clause.rfind(key)
        if p > best_pos:
            best_pos, best_sym = p, sym
    return best_sym


@_safe("fact_consistency")
def check_fact_consistency(db: Session, limit: int = 90) -> dict:
    """A2 数字一致性：检测首席在 market_summary 中编造数字。"""
    reports = _load_reports(db, limit)
    market = _latest_market_map(db)

    found, matched, unspecified, unverifiable = 0, 0, 0, 0
    mismatched = []
    unverifiable_samples = []

    for r in reports:
        summary = str(r["content"].get("market_summary", "") or "")
        if not summary:
            continue
        nums = []
        for m in PCT_RE.finditer(summary):
            nums.append(("pct", float(m.group(1)), m.start(), m.end()))
        for m in POINT_RE.finditer(summary):
            nums.append(("point", float(m.group(1)), m.start(), m.end()))

        for kind, val, s, e in nums:
            ctx_text = summary[max(0, s - 10):min(len(summary), e + 10)]
            if YEAR_RE.search(ctx_text) or ORD_RE.search(ctx_text):
                continue

            # 方向：中文用"涨/跌"字表意，取其符号
            prefix = summary[max(0, s - 6):s]
            signed = val
            if kind == "pct":
                if any(w in prefix for w in DOWN_WORDS) and not any(w in prefix for w in UP_WORDS):
                    signed = -val

            hedged = any(h in summary[max(0, s - 5):s] for h in HEDGE)
            tol = 0.5 if (kind == "pct" and hedged) else (0.15 if kind == "pct" else 2.0)

            # 解析主语（取子句内离数字最近的标的词）
            symbol = _resolve_subject(summary, s)

            def _hit(sym: str) -> bool:
                row = market.get(sym)
                if row is None:
                    return False
                target = row.change_pct if kind == "pct" else row.close
                if target is None:
                    return False
                return abs(float(target) - signed) <= tol

            if symbol is not None:
                # 主语明确 → 计入校验（对错都算）
                found += 1
                if _hit(symbol):
                    matched += 1
                else:
                    mismatched.append({
                        "date": str(r["date"].date()), "text": ctx_text.strip()[:60],
                        "value": signed, "kind": kind, "subject": symbol,
                    })
            else:
                # 主语未解析 → 先试全部标的；命中算过，未命中记为"不可校验"而非错
                unspecified += 1
                if any(_hit(s) for s in market):
                    found += 1
                    matched += 1
                else:
                    unverifiable += 1
                    unverifiable_samples.append({
                        "date": str(r["date"].date()),
                        "text": ctx_text.strip()[:60], "value": signed, "kind": kind,
                    })

    if found == 0:
        return {"check": "fact_consistency", "status": "skipped",
                "reason": "未提取到可校验（主语明确）的数字",
                "unverifiable": unverifiable}

    rate = round(matched / found, 3)
    result = {"check": "fact_consistency", "status": "ok",
              "reports_scanned": len(reports), "numbers_found": found,
              "matched": matched, "match_rate": rate,
              "unspecified_subject": unspecified,
              "unverifiable": unverifiable,
              "unverifiable_samples": unverifiable_samples[:5],
              "mismatched": mismatched[:10]}
    if rate < 0.9:
        result["flag"] = "存在编造数字风险"
    return result


@_safe("risk_level_distribution")
def check_risk_level_distribution(db: Session, limit: int = 180) -> dict:
    """A3 风险等级分布：捕捉风险官退化的**两个方向**。

    只盯"永远判高"是不够的 —— 等级锚点放开后，退化可能摆向另一头：
    "永远判低"同样让降档机制失去区分度，只是换成了「不起作用」而非「天天起作用」。
    两个方向都会让 `risk_veto` 退化为常数，故一并监控。
    """
    reports = _load_reports(db, limit)
    levels = []
    for r in reports:
        lv = (r["content"].get("risk_opinion") or {}).get("risk_level")
        if lv:
            levels.append(lv)

    if not levels:
        return {"check": "risk_level_distribution", "status": "skipped",
                "reason": "无报告包含 risk_opinion.risk_level"}

    dist = {k: levels.count(k) for k in ("高", "中", "低")}
    ratio = round(dist["高"] / len(levels), 3)
    low_ratio = round(dist["低"] / len(levels), 3)
    result = {"check": "risk_level_distribution", "status": "ok",
              "total": len(levels), "distribution": dist,
              "high_ratio": ratio, "low_ratio": low_ratio}
    if len(levels) < 5:
        result["status"] = "insufficient_data"
        result["reason"] = f"仅 {len(levels)} 份样本（需 ≥5），当前分布供观察"
    elif ratio > 0.8:
        result["flag"] = "风险官可能过度谨慎，降档机制趋于常态化"
    elif low_ratio > 0.8:
        result["flag"] = "风险官可能退化为走过场，降档机制失去区分度"
    return result


# =====================================================================
# 阶段 Q —— 数据质量
# =====================================================================
@_safe("category_coverage")
def check_category_coverage(db: Session) -> dict:
    """Q1 类别覆盖：哪些类别无专家认领；归一化是否漏网。"""
    from app.agent.data_agent import CATEGORY_ALIAS

    rows = db.query(News.category).all()
    counts: dict = {}
    for (c,) in rows:
        counts[c or "(空)"] = counts.get(c or "(空)", 0) + 1
    total = sum(counts.values())

    CLAIMED = {"宏观": "宏观", "政策": "宏观", "行业": "行业",
               "公司": "行业", "资金": "资金面", "市场": "资金面", "综合": "(广播)"}
    detail = {
        k: {"count": v, "pct": round(v / total, 3) if total else 0,
            "claimed_by": CLAIMED.get(k)}
        for k, v in sorted(counts.items(), key=lambda x: -x[1])
    }
    unclaimed = sum(v for k, v in counts.items() if k not in CLAIMED)

    # 归一化漏网：CATEGORY_ALIAS 的 key 不应出现在最终 category 中
    leaked = {k: counts[k] for k in CATEGORY_ALIAS if k in counts}

    result = {"check": "category_coverage", "status": "ok", "total": total,
              "distribution": detail,
              "unclaimed_count": unclaimed,
              "unclaimed_pct": round(unclaimed / total, 3) if total else 0,
              "alias_leaked": leaked}
    if leaked:
        result["flag"] = f"归一化漏网：{leaked}（应为修复前的历史残留）"
    return result


@_safe("retrieval_health")
def check_retrieval_health(db: Session | None = None) -> dict:
    """Q2 检索与日历健康：两个「静默降级」部件的可用性。

    - BM25 索引为空 → RAG 退化为纯向量检索（关键词精确匹配能力缺失）
    - 交易日历降级 → 按「工作日」猜测，节假日可能被误判为交易日

    这两者失效后系统都照常运行，只是质量下降且无感知，故单列检查。
    """
    from app.collectors.trading_calendar import status as calendar_status
    from app.rag.retriever import get_retriever

    r = get_retriever()
    cal = calendar_status()

    result = {
        "check": "retrieval_health", "status": "ok",
        "bm25_ready": r.is_ready(), "bm25_size": r.size,
        "calendar_loaded": cal["loaded"],
        "calendar_degraded": cal["degraded"],
    }
    flags = []
    if not r.is_ready():
        flags.append("BM25 索引为空，RAG 已退化为纯向量检索")
    if cal["degraded"]:
        flags.append("交易日历降级中，节假日可能被误判为交易日")
    elif not cal["loaded"]:
        # 区分「检查过且正常」与「还没检查过」——后者不可当成健康
        flags.append("交易日历尚未加载、状态未知（可能未预热）")
    if flags:
        result["flag"] = "；".join(flags)
    return result


# =====================================================================
# 阶段 B —— 骨架（等数据量）
# =====================================================================
def _has_column(table: str, column: str) -> bool:
    from app.db import engine
    try:
        return column in {c["name"] for c in inspect(engine).get_columns(table)}
    except Exception:  # noqa: BLE001
        return False


@_safe("risk_officer_calibration")
def check_risk_officer_calibration(db: Session, limit: int = 180) -> dict:
    """B1 风险官校准：veto 组 vs 非 veto 组的次日表现差异。"""
    if not _has_column("report", "risk_veto"):
        return {"check": "risk_officer_calibration", "status": "skipped",
                "reason": "等待 EXPERTS_DESIGN.md §7.3 的字段迁移"}

    reports = _load_reports(db, limit)
    groups = {True: [], False: []}
    for r in reports:
        if r.get("risk_veto") is None:
            continue
        st = _next_day_stats(db, r["date"])
        if st:
            groups[bool(r["risk_veto"])].append(st)

    if len(groups[True]) < 20 or len(groups[False]) < 20:
        return {"check": "risk_officer_calibration", "status": "insufficient_data",
                "reason": f"veto组 n={len(groups[True])}，非veto组 n={len(groups[False])}，均需 ≥20"}

    def _agg(items):
        chg = [abs(i["change_pct"]) for i in items if i["change_pct"] is not None]
        amp = [i["amplitude"] for i in items if i["amplitude"] is not None]
        down = [i["change_pct"] for i in items if i["change_pct"] is not None and i["change_pct"] < 0]
        return {"n": len(items),
                "abs_change_mean": round(sum(chg) / len(chg), 3) if chg else None,
                "amplitude_mean": round(sum(amp) / len(amp), 4) if amp else None,
                "down_ratio": round(len(down) / len(chg), 3) if chg else None}

    a, b = _agg(groups[True]), _agg(groups[False])
    from scipy.stats import mannwhitneyu

    va = [abs(i["change_pct"]) for i in groups[True] if i["change_pct"] is not None]
    vb = [abs(i["change_pct"]) for i in groups[False] if i["change_pct"] is not None]
    p = float(mannwhitneyu(va, vb, alternative="two-sided").pvalue)

    verdict = ("风险官有预测力" if (p < 0.05 and (a["abs_change_mean"] or 0) > (b["abs_change_mean"] or 0))
               else "veto 可能为噪声，系统在无理由地降低置信度")
    return {"check": "risk_officer_calibration", "status": "ok",
            "veto_group": a, "non_veto_group": b, "p_value": round(p, 4),
            "verdict": verdict}


@_safe("risk_officer_overcaution")
def check_risk_officer_overcaution(db: Session, limit: int = 180) -> dict:
    """B2 过度谨慎：风险官否决看多后，次日实际上涨的比例。"""
    if not _has_column("report", "risk_veto"):
        return {"check": "risk_officer_overcaution", "status": "skipped",
                "reason": "等待 EXPERTS_DESIGN.md §7.3 的字段迁移"}

    reports = _load_reports(db, limit)
    ups, tot = 0, 0
    for r in reports:
        if not r.get("risk_veto") or r.get("sentiment") != "偏多":
            continue
        st = _next_day_stats(db, r["date"])
        if st and st["change_pct"] is not None:
            tot += 1
            ups += int(st["change_pct"] > 0)

    if tot == 0:
        return {"check": "risk_officer_overcaution", "status": "insufficient_data",
                "reason": "无「risk_veto=True 且 sentiment=偏多」的样本"}
    ratio = round(ups / tot, 3)
    result = {"check": "risk_officer_overcaution", "status": "ok",
              "samples": tot, "up_ratio": ratio}
    # 最小样本保护：n=1 且次日恰好上涨 → ratio=1.0 > 0.55，会直接误报。
    # 10 是**最低可判**线，不是统计显著线（n=10 照样说明不了什么，只是比 n=1 强）。
    # 样本不够时仍返回比值供观察，只是不下结论 —— 与 A3 同一口径。
    if tot < 10:
        result["status"] = "insufficient_data"
        result["reason"] = f"仅 {tot} 个样本（需 ≥10），当前比值供观察"
    elif ratio > 0.55:
        result["flag"] = "可能系统性误伤看多判断"
    return result


# =====================================================================
# 阶段 C —— 未实现
# =====================================================================
@_safe("attribution_consistency")
def check_attribution_consistency(db: Session, limit: int = 90) -> dict:
    """C 归因一致性：key_drivers 声称的板块，次日是否真的跑赢。

    数据源已就绪（sector_data 表，自 2026-09-16 起采集），但历史累积极少，
    不足以做归因统计，故仍不实现。实现时需：
      1. 从 key_drivers 抽取板块名（需与 sector_data.name 做映射）
      2. 取该板块**次日**涨跌幅，与大盘对比
      3. 统计"声称的板块是否真的跑赢"
    """
    return {"check": "attribution_consistency", "status": "skipped",
            "reason": "数据源已就绪（sector_data），但历史累积不足，暂不实现"}


# =====================================================================
# 聚合与日志
# =====================================================================
def run_all_checks(db: Session, limit: int = 90,
                   include_diagnostic: bool = False) -> dict:
    """运行全部检查。

    include_diagnostic=True 时才跑 S1/S2（它们会额外调用 LLM，且结果不逐日变化）。
    """
    checks = [
        check_risk_specificity(db, limit),
        check_chief_faithfulness(db, limit),
        check_internal_consistency(db, limit),
        check_risk_grounding(db, limit),
        check_output_diversity(db, limit),
        check_reference_traceability(db, limit),
        check_fact_consistency(db, limit),
        check_risk_level_distribution(db),
        check_category_coverage(db),
        check_retrieval_health(db),
        check_risk_officer_calibration(db),
        check_risk_officer_overcaution(db),
        check_attribution_consistency(db, limit),
    ]
    if include_diagnostic:
        checks.insert(0, check_sensitivity(db))
        checks.insert(1, check_reproducibility(db))

    return {
        "generated_at": datetime.now().isoformat(),
        "include_diagnostic": include_diagnostic,
        "checks": {c.get("check", "unknown"): c for c in checks},
    }


def append_eval_log(db: Session, path: str = "data/eval_log.jsonl") -> dict:
    """把评估结果以追加方式写入 JSONL（不覆盖、不截断）。"""
    from pathlib import Path

    result = run_all_checks(db)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result, ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    logger.info(f"评估日志已追加：{path}")
    return {"path": str(p.resolve()), "generated_at": result["generated_at"],
            "bytes": len(line.encode("utf-8"))}
