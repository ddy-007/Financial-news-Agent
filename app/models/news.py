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
    # 多源佐证（专家研判不据此加权，但 `assess_info_level` 的信号②用它）。
    # ⚠️ 口径 = **去重后的源数**（不是「(源, url) 对数」）—— 2026-09-22 修正：
    # 原先数来源对，导致同一家挂两个 url 就成了「2源」（实测库里 332 行如此）。
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    source_urls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    # 【已废弃】新闻级情绪分：-1(极空) ~ 1(极多)，0 中性。
    # 2026-09-22 起**不再产生新值**（打分环节已整体删除），但**字段与历史值保留**
    # 以便回溯 —— 当年打过的 2800 条仍在库里。`summary` 是同期产出的「理由」，一并停写。
    # 消费方已全部摘除：`assess_info_level` 的信号②b 已删、`routes_news` 恒返回 None。
    # 将来若要彻底清理，需连同 config.py 的两个废弃字段与 .env 一起动。
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
