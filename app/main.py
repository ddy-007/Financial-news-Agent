"""FastAPI 入口：注册路由、初始化数据库、启动调度器。"""
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api import (
    routes_agent,
    routes_market,
    routes_news,
    routes_reports,
    routes_sectors,
)
from app.collectors.scheduler import start_scheduler, stop_scheduler
from app.db import init_db


def _warm_up() -> None:
    """启动预热：重建 BM25 索引 + 检查交易日历。

    不做预热的后果：进程刚启动时 BM25 为空，RAG 会**静默退化为纯向量检索**，
    直到下一次新闻采集（最长 30 分钟）才恢复。
    """
    from app.collectors.trading_calendar import is_degraded
    from app.db import SessionLocal
    from app.rag.retriever import get_retriever
    from app.services.news_service import rebuild_bm25_index

    db = SessionLocal()
    try:
        rebuild_bm25_index(db)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"BM25 索引预热失败: {e}")
    finally:
        db.close()

    r = get_retriever()
    if r.is_ready():
        logger.info(f"BM25 索引预热完成，共 {r.size} 条")
    else:
        logger.warning("BM25 索引为空（库中近期无新闻？），RAG 将退化为纯向量检索")

    if is_degraded(ensure_loaded=True):
        logger.warning("交易日历处于降级状态：将按「工作日」判断交易日，节假日可能误判")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("数据库初始化完成")
    # 后台线程预热：akshare 联网无超时，放启动路径可能拖慢甚至卡住启动
    threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()
    start_scheduler()  # 如需关闭自动定时采集，注释本行
    yield
    stop_scheduler()


app = FastAPI(title="金融新闻情报简报 Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_reports.router)
app.include_router(routes_news.router)
app.include_router(routes_market.router)
app.include_router(routes_sectors.router)
app.include_router(routes_agent.router)


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}
