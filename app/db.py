"""数据库连接与会话管理（SQLite / SQLAlchemy）。"""
from pathlib import Path

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


def get_db():
    """FastAPI 依赖注入用的会话生成器。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
