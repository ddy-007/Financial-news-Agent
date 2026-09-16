"""每日研判报告表。"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketReport(Base):
    __tablename__ = "report"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    date: Mapped[datetime] = mapped_column(DateTime, index=True, unique=True)
    # 报告类型：daily(日报) / weekly(周报)
    report_type: Mapped[str | None] = mapped_column(
        String(10), index=True, default="daily", nullable=True
    )
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)  # 完整报告（markdown）
    # 结构化字段，便于回测与统计
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 偏多/中性/偏空
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)  # high/medium/low
    score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 综合打分 -1~1
    # —— 多专家分析新增字段 ——
    expert_opinions: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: 5位专家观点
    divergence: Mapped[float | None] = mapped_column(Float, nullable=True)  # 分歧度 0~2
    risk_veto: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # 风险官是否触发降档
    # 信息量标记：True 表示当天四个信号均未触发（市场平静），报告仍生成但内容精简
    low_info: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 数据时效标记：True 表示近期新闻不足、本次研判基于陈旧数据（非当日）
    data_stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
