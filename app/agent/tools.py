"""Agent 工具集：RAG 检索 + 行情查询 + 新闻浏览 + 历史报告 + 联网搜索。"""
import httpx
from langchain_core.tools import tool

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


@tool
def get_market_overview() -> str:
    """获取主要指数（A股+美股）最新行情：名称、收盘价、涨跌幅。用于了解当前市场整体表现。"""
    db = SessionLocal()
    try:
        rows = db.query(MarketData).order_by(MarketData.date.desc()).all()
        latest: dict[str, MarketData] = {}
        for r in rows:
            latest.setdefault(r.symbol, r)
        lines = [
            f"{r.name}({r.symbol}): 收盘 {r.close}，涨跌幅 {r.change_pct}%，"
            f"日期 {r.date.date()}"
            for r in latest.values()
        ]
        return "\n".join(lines) if lines else "暂无行情数据。"
    finally:
        db.close()


@tool
def get_recent_news(keyword: str = "", category: str = "", limit: int = 10) -> str:
    """按关键词或分类查询最近新闻。category 取 宏观/政策/行业/公司/国际/资金 之一（可空）。"""
    db = SessionLocal()
    try:
        q = db.query(News).order_by(News.publish_time.desc()).limit(limit)
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
            .limit(limit)
            .all()
        )
        lines = [
            f"- {r.date.date()} 情绪{r.sentiment} score={r.score}: {r.title}"
            for r in rows
        ]
        return "\n".join(lines) if lines else "暂无历史报告。"
    finally:
        db.close()


ALL_TOOLS = [search_news, get_market_overview, get_recent_news, get_report_history, web_search]
