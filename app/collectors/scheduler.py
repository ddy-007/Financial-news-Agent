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


def start_scheduler() -> None:
    if scheduler.running:
        return
    # 新闻：每 30 分钟增量采集
    scheduler.add_job(_job_news, CronTrigger(minute="*/30"), id="news_collect")
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
