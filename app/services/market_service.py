"""行情业务：采集 + 去重入库。"""
from __future__ import annotations

import threading

from loguru import logger
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.collectors.market_collector import collect_all_market_data
from app.models.market import MarketData


def save_market_data(db: Session, rows: list[dict]) -> int:
    """按 `(symbol, date)` 幂等入库，返回**真正新增**的条数。

    ⚠️ 2026-09-25 改（第三方巡检 M5）。原实现是「先查后插」：

        exists = db.query(...).first()
        if exists: continue
        db.add(MarketData(**r))

    **两个独立的问题**：
      ① **竞态**：两个请求（或手动 + 定时同时触发）都查不到 → 都 `add` →
         后提交者撞唯一约束 `uq_symbol_date` → **抛 IntegrityError**。
         而原实现**完全不捕获它**，异常会一路冒到 API → 500，且**事务已脏**
         （会话不 rollback 就没法再用）。
      ② **逐条 commit 的代价**：`db.commit()` 在循环**外**，所以其实是「逐条 add + 一次性
         commit」—— 这还好；但整批共用一个事务，撞一次异常会**连累整批**。

    改为数据库原生 `ON CONFLICT DO NOTHING`（SQLite 3.24+）：**冲突在库内解决**，
    不再有「查与插之间的窗口」，也不需要 try/except + rollback 的补丁。
    代价只有一次 INSERT 语句，比原来「每行一条 SELECT」还快。
    """
    if not rows:
        return 0
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        insert = sqlite_insert
    elif dialect_name == "postgresql":
        insert = postgresql_insert
    else:
        raise RuntimeError(
            f"行情幂等写入暂不支持数据库方言: {dialect_name}"
        )
    stmt = insert(MarketData).on_conflict_do_nothing(
        index_elements=["symbol", "date"]
    )
    # ⚠️ 必须走 `db.connection()`（Core 连接）而不是 `db.execute()`：
    # SQLAlchemy 2.0 对「ORM 实体 + 参数列表」的 execute 返回 **`IteratorResult`**，
    # **没有 `rowcount`** —— 实测直接 `AttributeError`。Core 路径才给 `CursorResult`。
    result = db.connection().execute(stmt, rows)
    db.commit()
    # `rowcount` 是**真正插入**的行数；被冲突挡掉的不计入 ——
    # 返回值语义（「新增条数」）与改动前一致。
    return result.rowcount or 0


# 行情采集互斥锁（**进程级**，与新闻侧的 `_COLLECT_LOCK` 同款）。
#
# 行情有两个触发方：APScheduler 的 `_job_market`（17:30）与
# `POST /api/v1/market/collect` 的手动端点 —— 与原新闻侧一模一样的结构，
# 而新闻侧早就因为 2026-09-22 那次「手动轮 + 定时轮重叠」加了锁，行情侧漏了。
#
# 现在即使真撞上，`ON CONFLICT DO NOTHING` 也保证**不会写坏数据**；
# 加锁是为了别做无用功（重复拉 akshare / yfinance、重复占用 SQLite 写锁）。
# 注意它只挡**同一进程内**的并发；多进程部署需要文件锁或 DB 锁。
_MARKET_LOCK = threading.Lock()


def collect_and_store_market(db: Session) -> int:
    """采集 + 入库（供调度器与手动端点调用），返回新增条数。

    拿不到锁就**跳过并返回 0** —— 与新闻侧同样的取舍：一轮采集要拉外部接口，
    排队等它没有意义（调用方靠日志区分「跳过了」与「真的没新增」）。
    """
    if not _MARKET_LOCK.acquire(blocking=False):
        logger.warning(
            "[行情] 已有一次采集在进行中，本次**跳过**（不排队）。"
            "手动端点与 17:30 的定时任务会撞上"
        )
        return 0
    try:
        rows = collect_all_market_data()
        return save_market_data(db, rows)
    finally:
        _MARKET_LOCK.release()
