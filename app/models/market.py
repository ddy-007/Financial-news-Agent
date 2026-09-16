"""行情表 + 宏观数据表。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketData(Base):
    __tablename__ = "market_data"
    __table_args__ = (UniqueConstraint("symbol", "date", name="uq_symbol_date"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    symbol: Mapped[str] = mapped_column(String(30), index=True)  # 如 000001.SH
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 涨跌幅%
    turnover: Mapped[float | None] = mapped_column(Float, nullable=True)  # 成交额


class MacroData(Base):
    __tablename__ = "macro_data"
    __table_args__ = (UniqueConstraint("name", "publish_date", name="uq_macro_name_date"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), index=True)  # CPI / PMI / LPR
    value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    publish_date: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    freq: Mapped[str | None] = mapped_column(String(20), nullable=True)  # monthly/quarterly
