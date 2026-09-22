"""FastAPI 入口：注册路由、初始化数据库、启动调度器。"""
import threading
from contextlib import asynccontextmanager
from pathlib import Path

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
from app.collectors.http_client import close_client
from app.collectors.scheduler import start_scheduler, stop_scheduler
from app.db import init_db

# 日志文件 sink 是否已挂。见 `_setup_file_logging` 的说明。
_LOG_SINK_ADDED = False


def _setup_file_logging() -> None:
    """把日志同时写一份到 `logs/app.log`。**幂等**。

    采集层从 2026-09-22 起把「脏记录」与「截断告警」**只记日志、不建表**
    （设计 §4.2），所以日志必须落盘 —— 在此之前全项目的 `logger` 只输出到控制台，
    进程一退就什么都没有。

    ⚠️ **为什么必须幂等**：loguru 的 `logger.add()` 每次调用都**真的新增一个 sink**，
    不是"有就跳过"。而 lifespan 在 `uvicorn --reload`、多次 startup、测试里会**反复执行**
    —— 不设防的话同一行日志会被写 N 次，而且很难察觉。
    """
    global _LOG_SINK_ADDED
    if _LOG_SINK_ADDED:
        return
    Path("logs").mkdir(exist_ok=True)
    logger.add("logs/app.log", rotation="10 MB", retention="14 days",
               encoding="utf-8", enqueue=True)
    _LOG_SINK_ADDED = True


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
    _setup_file_logging()
    init_db()
    logger.info("数据库初始化完成")
    # 后台线程预热：akshare 联网无超时，放启动路径可能拖慢甚至卡住启动
    threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()
    start_scheduler()  # 如需关闭自动定时采集，注释本行
    yield
    stop_scheduler()
    close_client()  # 关闭采集层共享的 httpx 连接池


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
