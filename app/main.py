"""FastAPI 入口：注册路由、初始化数据库、启动调度器。"""
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
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
    """**存活探针**（liveness）：只表示「进程还在」。

    ⚠️ 它**不检查任何依赖** —— 数据库断了、BM25 空了、调度器没起来，它照样返回
    `ok`。这是**刻意**的：把依赖检查塞进存活探针，会变成「进程没死但探针失败 →
    被反复重启」的经典事故。要判断「能不能正经干活」，用 `/api/v1/health/ready`。
    """
    return {"status": "ok"}


def _readiness_status(checks: dict) -> str:
    """由各依赖状态推出 `ok` / `degraded` / `unavailable`。**纯函数，可测**。

    **为什么分三档而不是二值**（2026-09-25 新增，应巡检 M8）：
    原来只有一个返回常量的 `/health`，「进程活着但核心功能失效」与「一切正常」
    在监控眼里**完全一样**，故障发现被推迟。

    ⚠️ **只有数据库失败才算 `unavailable`**（503）：
      · 数据库是唯一的硬依赖 —— 它挂了，任何接口都做不了事；
      · BM25 为空只是**检索质量降级**（退化为纯向量），服务照常可用；
      · 调度器没跑只是「不再自动采集」，手动接口仍然工作。
    把后两者也算成 503，会让负载均衡在**服务其实能用**的时候把它摘掉 ——
    那比不报更糟。它们以 `degraded` 如实呈现，不掩盖。
    """
    if checks.get("database") is not True:
        return "unavailable"
    if checks.get("bm25_ready") is not True or checks.get("scheduler_running") is not True:
        return "degraded"
    return "ok"


@app.get("/api/v1/health/ready")
def readiness(response: Response):
    """**就绪探针**：核心依赖是否可用（巡检 M8）。

    每个依赖**单独报状态**，不合成一个布尔 —— 「哪一项坏了」比「坏了」有用得多。
    """
    checks: dict = {}

    # ① 数据库：真发一条最轻的查询，不是看 engine 对象在不在
    try:
        from sqlalchemy import text as _text

        from app.db import SessionLocal
        _db = SessionLocal()
        try:
            _db.execute(_text("SELECT 1"))
            checks["database"] = True
        finally:
            _db.close()
    except Exception as e:  # noqa: BLE001
        checks["database"] = f"{type(e).__name__}: {e}"

    # ② BM25（RAG 的关键词路）：空索引会让检索**静默**退化为纯向量
    try:
        from app.rag.retriever import get_retriever
        _r = get_retriever()
        checks["bm25_ready"] = _r.is_ready()
        checks["bm25_size"] = _r.size
    except Exception as e:  # noqa: BLE001
        checks["bm25_ready"] = f"{type(e).__name__}: {e}"

    # ③ 调度器：定时采集 / 研判全靠它
    try:
        from app.collectors.scheduler import scheduler
        checks["scheduler_running"] = bool(scheduler.running)
    except Exception as e:  # noqa: BLE001
        checks["scheduler_running"] = f"{type(e).__name__}: {e}"

    status = _readiness_status(checks)
    if status == "unavailable":
        response.status_code = 503
    return {"status": status, "checks": checks}
