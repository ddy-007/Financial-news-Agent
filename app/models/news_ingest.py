"""新闻原文处理队列与历史覆盖缺口；不参与报告/RAG 查询。"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RawNews(Base):
    __tablename__ = "raw_news"

    # 完整 NewsItem 内容的摘要；同 URL 正文更新也保留为独立版本。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    news_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CollectionGap(Base):
    __tablename__ = "collection_gap"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
