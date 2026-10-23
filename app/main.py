"""FastAPI 入口：注册路由、初始化数据库、启动调度器。"""
import logging
import sys
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


class _InterceptHandler(logging.Handler):
    """把**标准库 logging** 的记录转发给 loguru。

    **为什么必须有**（2026-10-22 实测）：APScheduler 全程用 stdlib logging
    （`apscheduler.executors.base` 打「Run time of job ... was missed by ...」、
    `apscheduler.scheduler` 打「Job ... raised an exception」）。而本项目只给
    loguru 挂了文件 sink，**没有这条桥** —— 于是这些消息在 root logger 上
    无人接收，只剩 `logging.lastResort` 写到 stderr。后果：

  · 2026-10-21 18:30 那次研判 misfire（迟到 2.04 秒被丢弃、当天没有日报），
    关键证据只出现在用户的控制台窗口里，`logs/app.log` 一片空白 ——
    事后翻日志只能看到「18:01 采集完成 → 18:53 重启」，中间完全查不出原因。

    ⚠️ 这也解释了排查时的困境：**日志里没有痕迹，不等于没有发生**。

    实现照抄 loguru 官方文档的 InterceptHandler，`depth` 用 6 是为了让
    `{name}:{function}:{line}` 指向**真正的调用点**而不是本文件。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # **找出真正的调用帧**，别用固定 depth（2026-10-23 审查发现 L1）。
        #
        # 固定 `depth=6` 对 `logging.warning/error/info` 恰好正确，但
        # `logger.exception()` 内部先转调 `Logger.error`（多一层栈帧），
        # 于是溯源会错到 `logging:exception:1524` —— 而 APScheduler 报
        # 「Job raised an exception」用的正是 `logger.exception()`。
        #
        # ⚠️ 实测调用链（从 emit 往外，`is_logging_file` 表示该帧在 logging 包里）：
        #   0 emit(self, 本文件, False)   ← **起点是 False**
        #   1 handle        (True)
        #   2 callHandlers  (True)
        #   3 handle        (True)
        #   4 _log          (True)
        #   5 warning       (True)
        #   6 real_caller   (False)       ← 真正想要的
        #
        # 所以**不能**用「从 currentframe 开始，遇到非 logging 帧就停」——
        # 第 0 帧就是非 logging 帧，会立刻停住、得到 logging:callHandlers。
        # 正确做法：先跳过**起点自身**（emit），再从下一个帧开始找
        # 「第一个不在 logging 包内的帧」。
        depth = 2
        frame = logging.currentframe().f_back   # 跳过 emit 自身
        while frame is not None:
            if frame.f_code.co_filename != logging.__file__:
                break
            frame = frame.f_back
            depth += 1
        # 把异常信息一并带过去，否则 `logger.exception` 的内容会丢
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _bridge_stdlib_logging() -> None:
    """把 stdlib logging 接到 loguru。**幂等**（可被 lifespan 反复调用）。

    只为第三方库（APScheduler / uvicorn / httpx）服务 —— 本项目自己的代码
    一律用 loguru。级别定在 WARNING：stdlib 的 INFO 大多是噪音
    （uvicorn 的访问日志本项目已有自己的写法）。
    """
    root = logging.getLogger()
    if any(isinstance(h, _InterceptHandler) for h in root.handlers):
        return
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.WARNING,
                        force=True)
    # APScheduler 与 uvicorn 都有自己的 logger，且 `propagate=False` 的话
    # 上面那条 basicConfig 管不到它们，要逐个接。
    for name in ("apscheduler", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [_InterceptHandler()]
        lg.propagate = False
        # ⚠️ **必须显式设 level**（2026-10-23 审查发现 M2）。
        # uvicorn 自己的 `dictConfig` 把 `uvicorn.access` 设成 INFO(20)，
        # 而本函数 docstring 说的是「级别定在 WARNING，访问日志是噪音」。
        # 只换 handler 不降 level 的话，每条 HTTP 访问日志都会灌进 app.log
        #（实测已累积 14 条 `GET /api/v1/health 200`），把 10MB 轮转撑快，
        # 也会淹没真正有用的告警。
        lg.setLevel(logging.WARNING)


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
    # 默认 stderr sink 也会展开变量；仅替换它，保留调用方的其他处理器。
    # 默认 sink 被禁用时不重建。
    try:
        logger.remove(0)
    except ValueError:
        pass
    else:
        logger.add(sys.stderr, diagnose=False, backtrace=True)
    Path("logs").mkdir(exist_ok=True)
    # ⚠️ **`diagnose=False` 不能省**（2026-10-23 审查发现，S1）。
    #
    # loguru 的 `diagnose` 默认是 **True**，它会把异常发生处**每一帧的局部变量**
    # 展开成 `│ └ 值` 写进 sink。而本项目这轮改动新增了
    # `_job_report` 的 `logger.opt(exception=True).error(...)` + stdlib→loguru 桥，
    # 使 LLM 调用的 traceback **第一次真正落盘** —— 于是：
    #
    #   `_make_client(api_key="sk-...", base_url=...)` 在任何构造/调用失败时，
    #   它的 `api_key` 形参会被完整展开写进 logs/app.log。
    #
    # 实测（2026-10-23，脱敏）：日志里出现
    #   `{'model': ..., 'api_key': 'sk-***', 'base_url': ...}`
    # 同一机制还让 `check_collector_contracts.py` 那条「失败日志不展开 SQL 参数」
    # 的契约失效（新闻正文会被展开）。
    #
    # `backtrace` 保留 True：它只展开**调用链**（函数名/行号），不含变量值，
    # 对排查有用且安全。
    logger.add("logs/app.log", rotation="10 MB", retention="14 days",
               encoding="utf-8", enqueue=True,
               diagnose=False,   # 关键：不展开局部变量，防明文密钥/新闻正文入盘
               backtrace=True)   # 保留完整调用链，只有函数名与行号
    # 顺序不能反：**先**挂文件 sink，**再**架 stdlib 桥 ——
    # 否则桥转发过来的第一批日志会落在没有文件 sink 的 loguru 上，仍然丢。
    _bridge_stdlib_logging()
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
        logger.warning(f"[就绪] 数据库检查失败：{type(e).__name__}: {e}")
        checks["database"] = "unavailable"

    # ② BM25（RAG 的关键词路）：空索引会让检索**静默**退化为纯向量
    try:
        from app.rag.retriever import get_retriever
        _r = get_retriever()
        checks["bm25_ready"] = _r.is_ready()
        checks["bm25_size"] = _r.size
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[就绪] BM25 检查失败：{type(e).__name__}: {e}")
        checks["bm25_ready"] = "unavailable"

    # ③ 调度器：定时采集 / 研判全靠它
    try:
        from app.collectors.scheduler import scheduler
        checks["scheduler_running"] = bool(scheduler.running)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[就绪] 调度器检查失败：{type(e).__name__}: {e}")
        checks["scheduler_running"] = "unavailable"

    status = _readiness_status(checks)
    if status == "unavailable":
        response.status_code = 503
    return {"status": status, "checks": checks}
