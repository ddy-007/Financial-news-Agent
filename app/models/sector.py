"""行业板块数据表。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SectorData(Base):
    """行业板块日频快照。

    唯一约束 `(date, name)` 即"去重"——同一板块同一天只存一条。
    这是存储层的正确性约束，与新闻的语义去重不同（板块是结构化数据，无需语义判重）。
    """

    __tablename__ = "sector_data"
    __table_args__ = (
        UniqueConstraint("date", "name", name="uq_sector_date_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    name: Mapped[str] = mapped_column(String(50), index=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    company_count: Mapped[float | None] = mapped_column(Float, nullable=True)
    leader: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 领涨股
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
