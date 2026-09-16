"""新闻表。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class News(Base):
    __tablename__ = "news"
    __table_args__ = (UniqueConstraint("url", name="uq_news_url"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(50), index=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    publish_time: Mapped[datetime | None] = mapped_column(
        DateTime, index=True, nullable=True
    )
    # 分类标签（由数据采集 Agent 填充）
    category: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)
    market: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    themes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 数组
    # 多源佐证（仅展示，不参与加权）
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    source_urls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    # 情绪分：-1(极空) ~ 1(极多)，0 中性；由 LLM 打分
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
