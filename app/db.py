"""数据库连接与会话管理（SQLite / SQLAlchemy）。"""
from pathlib import Path

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.base import Base

# 确保 SQLite 数据库文件所在目录存在（否则 sqlite 无法创建文件）
if settings.database_url.startswith("sqlite:///"):
    db_path = settings.database_url[len("sqlite:///"):]
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # SQLite 专用
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """建表 + 轻量迁移（幂等）。"""
    from app import models  # noqa: F401  触发模型注册

    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate() -> None:
    """给已有表补充缺失字段（SQLite 简单迁移）。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    additions = {
        "news": {
            "market": "ALTER TABLE news ADD COLUMN market VARCHAR(20)",
            "themes": "ALTER TABLE news ADD COLUMN themes TEXT",
            "source_count": "ALTER TABLE news ADD COLUMN source_count INTEGER DEFAULT 1",
            "source_urls": "ALTER TABLE news ADD COLUMN source_urls TEXT",
        },
        "report": {
            "expert_opinions": "ALTER TABLE report ADD COLUMN expert_opinions TEXT",
            "divergence": "ALTER TABLE report ADD COLUMN divergence FLOAT",
            "risk_veto": "ALTER TABLE report ADD COLUMN risk_veto BOOLEAN",
            "report_type": "ALTER TABLE report ADD COLUMN report_type VARCHAR(10) DEFAULT 'daily'",
            "low_info": "ALTER TABLE report ADD COLUMN low_info BOOLEAN",
            "data_stale": "ALTER TABLE report ADD COLUMN data_stale BOOLEAN",
            "report_day": "ALTER TABLE report ADD COLUMN report_day DATE",
        },
    }
    with engine.begin() as conn:
        for table, cols_ddl in additions.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in cols_ddl.items():
                if col not in existing:
                    conn.execute(text(ddl))
        if "report" in tables:
            _migrate_report_day(conn)


def _migrate_report_day(conn) -> int:
    """让「一天一份日报」**真正生效**（H2，2026-09-25）。返回删掉的重复行数。

    **为什么需要**：`report.day` 存的是**生成时刻**（含微秒的 datetime），唯一约束
    作用在它上面，同一天不同秒就是不同值 —— 「一天一份」实际没约束住。
    实测库里 `2026-09-15` 有 3 份日报、`09-14`/`09-06` 各 2 份。

    ⚠️ **四步顺序不能换**：
      ① 补 `report_day` 列（在 `additions` 里做）
      ② 回填：`report_day = date(date)`
      ③ 把 `report_type` 的 NULL 补成 `'daily'` —— SQLite 的唯一索引**认为 NULL 互不相等**，
         留着 NULL 等于在索引上开了个洞，将来还能落进同日重复
      ④ **清理历史重复**（保留每天最晚那份）
      ⑤ **最后**才建唯一索引 —— 有重复时直接建会失败

    幂等：每一步都用 `IF NOT EXISTS` / `WHERE` 护栏，重复执行无副作用。
    """
    # ⚠️ `text` 在本文件里是**函数内局部导入**（见 `_migrate`），模块级拿不到 ——
    # 第一版漏了这行，实测直接 `NameError`。
    from sqlalchemy import text

    # ② 回填业务日。SQLite 的 `date()` 能从 'YYYY-MM-DD HH:MM:SS.ffffff' 取出日期部分。
    conn.execute(text(
        "UPDATE report SET report_day = date(date) WHERE report_day IS NULL"
    ))
    # ③ 补 NULL 的 report_type（否则唯一索引有洞）
    conn.execute(text(
        "UPDATE report SET report_type = 'daily' WHERE report_type IS NULL"
    ))
    # ④ 找出「同 (report_type, report_day) 里不是最晚」的行 —— 先留痕再删。
    #    判定用**时间戳更晚者胜**，与 `compute_backtest` 的按天取最后一份**同口径**。
    doomed = conn.execute(text(
        "SELECT id, date, report_type, report_day, score FROM report AS r "
        "WHERE EXISTS (SELECT 1 FROM report AS r2 "
        "              WHERE r2.report_type = r.report_type "
        "                AND r2.report_day = r.report_day "
        "                AND r2.date > r.date)"
    )).fetchall()
    for row in doomed:
        # **不建审计表**（只为这 5 行引入一张表不划算），改为把被覆盖者打进日志 ——
        # 可 grep、可追溯，且不会因为「顺手删了」而无声无息。
        logger.warning(
            f"[迁移] 同日重复报告被清理：id={row.id} date={row.date} "
            f"type={row.report_type} day={row.report_day} score={row.score} "
            f"—— 保留该日**最晚**的一份（与回测取最后一份同口径）"
        )
    if doomed:
        conn.execute(text(
            "DELETE FROM report AS r "
            "WHERE EXISTS (SELECT 1 FROM report AS r2 "
            "              WHERE r2.report_type = r.report_type "
            "                AND r2.report_day = r.report_day "
            "                AND r2.date > r.date)"
        ))
    # ⑤ 建唯一索引（放在最后：有重复时建索引会失败）
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_report_type_day "
        "ON report (report_type, report_day)"
    ))
    return len(doomed)


def get_db():
    """FastAPI 依赖注入用的会话生成器。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
