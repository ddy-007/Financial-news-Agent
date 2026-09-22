"""APScheduler 定时调度：新闻增量采集 / 行情采集 / 每日研判。"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.config import settings

scheduler = BackgroundScheduler()  # 使用系统本地时区（中国 = 北京时间）


def _job_news() -> None:
    from app.db import SessionLocal
    from app.services.news_service import collect_and_store_news

    db = SessionLocal()
    try:
        n = collect_and_store_news(db)
        logger.info(f"[调度] 新闻采集完成，新增 {n} 条")
    finally:
        db.close()


def _job_market() -> None:
    from app.collectors.trading_calendar import is_trading_day
    from app.db import SessionLocal
    from app.services.market_service import collect_and_store_market

    if not is_trading_day():
        logger.info("[调度] 今日非交易日，跳过行情采集")
        return
    db = SessionLocal()
    try:
        n = collect_and_store_market(db)
        logger.info(f"[调度] 行情采集完成，新增 {n} 条")
    finally:
        db.close()


def _job_sector() -> None:
    from app.collectors.trading_calendar import is_trading_day
    from app.db import SessionLocal
    from app.services.sector_service import collect_and_store_sectors

    if not is_trading_day():
        logger.info("[调度] 今日非交易日，跳过板块采集")
        return
    db = SessionLocal()
    try:
        n = collect_and_store_sectors(db)
        logger.info(f"[调度] 板块采集完成，入库 {n} 条")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[调度] 板块采集失败（不影响主流程）: {e}")
    finally:
        db.close()


def _job_report() -> None:
    from app.collectors.trading_calendar import is_trading_day
    from app.db import SessionLocal
    from app.services.report_service import run_daily_pipeline

    if not is_trading_day():
        logger.info("[调度] 今日非交易日，跳过每日研判")
        return
    db = SessionLocal()
    try:
        report = run_daily_pipeline(db)
        logger.info(f"[调度] 每日研判完成: {report.sentiment} score={report.score}")
    finally:
        db.close()


def _job_weekly() -> None:
    """周报：仅在**本周最后一个交易日**实际生成（自动适配假期缩短的周）。"""
    from app.collectors.trading_calendar import is_last_trading_day_of_week
    from app.db import SessionLocal
    from app.services.report_service import generate_weekly

    if not is_last_trading_day_of_week():
        logger.info("[调度] 今日非本周最后交易日，跳过周报生成")
        return
    db = SessionLocal()
    try:
        report = generate_weekly(db)
        if report is None:
            logger.info("[调度] 本周日报不足，未生成周报")
        else:
            logger.info(f"[调度] 周报生成完成: {report.sentiment} score={report.score}")
    finally:
        db.close()


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _interval_ok(n: int) -> bool:
    """该间隔能否表达成**整点对齐、节奏恒定**的 cron。

    两个条件缺一不可：

    · `n >= 15`                   —— 见下，「太密」不算合法。
    · `n % 60 == 0 or 60 % n == 0` —— 能表达成「每小时内的固定分钟点」
      （15 / 20 / 30）或「每整数小时」（60 / 120 / 180 …）。
    · `1440 % n == 0`             —— **一天能被铺满**，末尾不会短一截。

    第二个条件不能省。反例：**300 分钟**看着像"每 5 小时"，但 24 % 5 != 0 ——
    实测 `CronTrigger(hour="*/5")` 的间隔是 **{240, 300}**：一天最后一段只有 4 小时。
    那正是这个配置要消除的"漂移"，迟早会漂到报告时刻上。

    `n == 1440`（每天一次）也一并挡下：`hour="*/24"` 会**直接抛 `ValueError`**
    （步长 24 超出 hour 的 0~23 范围），顺着 `start_scheduler()` 把**后端启动
    一起掀翻** —— 这比漂移更糟，是启动失败。要"每天一次"请用 720（每 12 小时）。

    **为什么还要求 `n >= 15`**：`1 / 2 / 3 / 4 / 5 / 6 / 10 / 12` 这几个值同样
    "整点对齐且节奏恒定"，谓词不拦的话会静默生效。但一轮采集实测要跑 4~6 分钟
    （积压轮可到 45 分钟），间隔比这还短的话，下一次触发来临时上一轮还没结束，
    `_COLLECT_LOCK` 会把它跳掉 —— **实际节奏变成"能跑多快跑多快"，而不是你配的
    那个数字**。与其让它悄悄变成另一种节奏，不如回退并告警。

    所以接受集合恰好是 `15 / 20 / 30 / 60 / 120 / 180 / 240 / 360 / 480 / 720`，
    与 `config.py` / `.env.example` 里写的合法值清单**逐项一致**。
    """
    return n >= 15 and (n % 60 == 0 or 60 % n == 0) and 1440 % n == 0 and n < 1440


def _news_trigger() -> CronTrigger:
    """按 `NEWS_INTERVAL_MINUTES` 生成**整点对齐**的新闻采集触发器。

    合法值：`15 / 20 / 30 / 60 / 120 / 180 / 240 / 360 / 480 / 720`（分钟），
    判据见 `_interval_ok()`。其余值一律**回退为 60 并告警**，绝不静默降级成
    另一个间隔 —— 那样用户以为设成了 X，实际跑的是 Y。

    为什么必须整点对齐（详见 `app/config.py` 的 `news_interval_minutes`）：
    四个报告任务挤在 17:30–19:30，新闻轮要跑 4~20 分钟且写未开 WAL 的 SQLite。
    """
    n = settings.news_interval_minutes
    if not _interval_ok(n):
        logger.warning(
            f"NEWS_INTERVAL_MINUTES={n} 不支持 —— 需能整除 60 或为 60 的倍数、"
            f"且能整除 1440、并且小于 1440（合法值见 _interval_ok 的 docstring）。"
            f"回退为 60 分钟；否则会漂到报告时刻上与它们撞车"
        )
        n = 60
    if n < 60:
        return CronTrigger(minute=f"*/{n}")
    if n == 60:
        return CronTrigger(minute="0")
    return CronTrigger(hour=f"*/{n // 60}", minute="0")


def start_scheduler() -> None:
    if scheduler.running:
        return
    # 新闻：按 NEWS_INTERVAL_MINUTES 增量采集（默认 60 分钟整点）
    scheduler.add_job(_job_news, _news_trigger(), id="news_collect")
    # 行情：收盘后（工作日）
    h, m = _parse_hhmm(settings.market_collect_time)
    scheduler.add_job(
        _job_market, CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
        id="market_collect",
    )
    # 板块数据（工作日，且仅交易日实际执行）
    h, m = _parse_hhmm(settings.sector_collect_time)
    scheduler.add_job(
        _job_sector, CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
        id="sector_collect",
    )
    # 每日研判（工作日，且仅交易日实际执行）
    h, m = _parse_hhmm(settings.report_time)
    scheduler.add_job(
        _job_report, CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
        id="daily_report",
    )
    # 周度综述（工作日，且仅本周最后一个交易日实际执行）
    h, m = _parse_hhmm(settings.weekly_report_time)
    scheduler.add_job(
        _job_weekly, CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
        id="weekly_report",
    )
    scheduler.start()
    logger.info("调度器已启动（行情/研判/周报均按交易日历跳过非交易日）")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()
        logger.info("调度器已停止")
