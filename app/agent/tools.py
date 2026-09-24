"""Agent 工具集：RAG 检索 + 行情查询 + 新闻浏览 + 历史报告 + 联网搜索 + 板块排行。"""
from datetime import datetime, time

import httpx
from langchain_core.tools import tool
from sqlalchemy import func

from app.config import settings
from app.db import SessionLocal
from app.models.market import MarketData
from app.models.news import News
from app.models.report import MarketReport
from app.rag import get_retriever


@tool
def search_news(query: str) -> str:
    """语义检索相关金融新闻。输入自然语言查询（如"今天降准相关消息"），返回相关新闻的来源、时间与内容。"""
    hits = get_retriever().hybrid_search(query, top_k=5)
    if not hits:
        return "未检索到相关新闻。"
    lines = []
    for h in hits:
        meta = h.metadata
        lines.append(
            f"- [{meta.get('source', '')}] {meta.get('title', '')}\n  {h.text[:200]}"
            f"（时间 {meta.get('publish_time', '')}）"
        )
    return "\n".join(lines)


# Agent 工具的**硬上限**。这些参数是 **LLM 生成**的，不是人填的 ——
# 它可能给 `limit=-1`（SQLite 里等于**不限行数**）或一个极大的数。
# 工具层自己再夹一次，不指望调用方守规矩（2026-09-25，巡检 M7）。
_TOOL_MAX_ROWS = 50


def _clamp_limit(limit, *, hi: int = _TOOL_MAX_ROWS, fallback: int = 10) -> int:
    """把 LLM 传来的 limit 夹到 `[1, hi]`；不能转成整数就用 `fallback`。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return fallback
    return max(1, min(n, hi))


@tool
def get_market_overview() -> str:
    """获取主要指数（A股+美股）最新行情：名称、收盘价、涨跌幅。用于了解当前市场整体表现。"""
    db = SessionLocal()
    try:
        # ⚠️ **在 SQL 里取每个指数的最新一条**，不要把全表拉回来再在 Python 里
        # `setdefault`（2026-09-25 修，第三方巡检 L2）。原写法每问一次就把整张
        # 行情表物化一遍 —— 现在才 543 行无感，但它**随采集天数线性增长、无上限**。
        # 子查询写法兼容 SQLite（没有 `DISTINCT ON`），且能吃到
        # `UNIQUE(symbol, date)` 带来的复合索引。
        newest = (
            db.query(MarketData.symbol, func.max(MarketData.date).label("mx"))
            .group_by(MarketData.symbol)
            .subquery()
        )
        rows = (
            db.query(MarketData)
            .join(newest, (MarketData.symbol == newest.c.symbol)
                  & (MarketData.date == newest.c.mx))
            # 同一 symbol 同一时刻有多行时（数据质量问题，见行情采集的日期兜底）
            # 取 id 稳定排序，避免每次调用给出不同的那一条
            .order_by(MarketData.symbol.asc(), MarketData.id.asc())
            .all()
        )
        lines = [
            f"{r.name}({r.symbol}): 收盘 {r.close}，涨跌幅 {r.change_pct}%，"
            f"日期 {r.date.date()}"
            for r in rows
        ]
        return "\n".join(lines) if lines else "暂无行情数据。"
    finally:
        db.close()


@tool
def get_recent_news(keyword: str = "", category: str = "", limit: int = 10) -> str:
    """按关键词或分类查询最近新闻。category 取 宏观/政策/行业/公司/国际/资金 之一（可空）。"""
    db = SessionLocal()
    try:
        q = (db.query(News).order_by(News.publish_time.desc())
             .limit(_clamp_limit(limit)))
        if keyword:
            q = q.filter(News.title.contains(keyword))
        if category:
            q = q.filter(News.category == category)
        rows = q.all()
        lines = [
            f"- [{r.source}][{r.category}] {r.title}（{r.publish_time}）" for r in rows
        ]
        return "\n".join(lines) if lines else "暂无新闻。"
    finally:
        db.close()


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索最新消息。用于查询已入库新闻之外的最新信息（实时行情、突发新闻、政策动态等）。"""
    if not settings.tavily_api_key:
        return "未配置 TAVILY_API_KEY，无法联网搜索。请在 .env 中配置。"
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return "未搜索到结果。"
        lines = []
        for r in results:
            lines.append(
                f"- {r.get('title', '')}\n  {r.get('content', '')[:150]}"
                f"（{r.get('url', '')}）"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"搜索失败: {e}"


@tool
def get_report_history(limit: int = 5) -> str:
    """查询历史研判报告的摘要（日期、情绪、综合分）。用于了解过去的研判观点。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(MarketReport)
            .filter((MarketReport.report_type == "daily")
                    | (MarketReport.report_type.is_(None)))
            .order_by(MarketReport.date.desc())
            .limit(_clamp_limit(limit, fallback=5))
            .all()
        )
        lines = [
            f"- {r.date.date()} 情绪{r.sentiment} score={r.score}: {r.title}"
            for r in rows
        ]
        return "\n".join(lines) if lines else "暂无历史报告。"
    finally:
        db.close()


@tool
def get_sector_performance(date: str = "") -> str:
    """查询行业板块涨跌排行：哪些板块在涨、哪些在跌、领涨股是谁。

    用于回答"钱往哪个方向去了"这类问题——指数只告诉你大盘涨跌，
    板块才看得出结构（比如"大盘跌但半导体在涨"）。

    date 留空取库中最新一天；也可指定日期，格式 YYYY-MM-DD。

    ⚠️ **返回值第一行是数据日期。回答时必须把这个日期说出来** ——
    板块采集只在工作日进行，后端没开的那几天会断档，数据可能不是最新的。
    """
    from app.services.sector_service import get_latest_sectors

    db = SessionLocal()
    try:
        as_of = None
        if date.strip():
            try:
                d = datetime.strptime(date.strip(), "%Y-%m-%d").date()
            except ValueError:
                return f"日期格式不对：{date!r}，应为 YYYY-MM-DD。"
            as_of = datetime.combine(d, time.max)

        rows, day = get_latest_sectors(db, as_of=as_of)
        ranked = [r for r in rows if r.change_pct is not None]
        if not ranked:
            return "暂无板块数据（可能尚未采集过，或该日期之前没有数据）。"

        up = sum(1 for r in ranked if r.change_pct > 0)
        down = sum(1 for r in ranked if r.change_pct < 0)
        flat = len(ranked) - up - down
        # 板块数不足时**收窄窗口**，否则两侧会重叠——极端情况下"领跌"行里
        # 列出的其实是上涨板块，自相矛盾。做法与 format_sector_summary 一致。
        n = max(1, min(5, len(ranked) // 2)) if len(ranked) > 1 else 1
        top = ranked[:n]
        # 只有 1 个板块时不列"领跌"——那会和"领涨"是同一条，列两遍只会让人困惑
        bottom = ranked[-n:][::-1] if len(ranked) > 1 else []

        def fmt(r) -> str:
            # 只在**上涨**板块标注领涨股 —— 对下跌板块说"领涨"自相矛盾
            # （该字段是"板块内涨幅第一的个股"，板块整体下跌时它也可能在跌）
            leader = f"（领涨 {r.leader}）" if (r.change_pct > 0 and r.leader) else ""
            return f"{r.name} {r.change_pct:+.2f}%{leader}"

        breadth = f"全市场 {len(ranked)} 个板块：{up} 涨 / {down} 跌"
        if flat:
            breadth += f" / {flat} 平"     # 不写会让"涨+跌 ≠ 总数"看着像算错了
        lines = [f"数据日期：{day}", breadth,
                 "领涨：" + "、".join(fmt(r) for r in top)]
        if bottom:
            lines.append("领跌：" + "、".join(fmt(r) for r in bottom))
        return "\n".join(lines)
    finally:
        db.close()


ALL_TOOLS = [search_news, get_market_overview, get_recent_news,
             get_report_history, web_search, get_sector_performance]
