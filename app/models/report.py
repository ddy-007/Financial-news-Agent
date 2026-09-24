"""每日研判报告表。"""
import datetime as _dt
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketReport(Base):
    __tablename__ = "report"

    # 「一天一份」的真正约束（H2，2026-09-25）。
    # 与 `db._migrate_report_day()` 里那条 DDL **必须两处都有**：
    #   · 全新库 → 走 `create_all`，靠这里；
    #   · 既有库 → `create_all` 会跳过已存在的表（含索引），靠 `_migrate` 补。
    # 排练时就是靠「全新库没有这条索引」发现的这个洞。
    __table_args__ = (
        Index("uq_report_type_day", "report_type", "report_day", unique=True),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    date: Mapped[datetime] = mapped_column(DateTime, index=True, unique=True)
    # **业务日** —— 「一天一份」的唯一依据（2026-09-25 新增，应巡检 H2）。
    #
    # ⚠️ **为什么不能只用 `date`**：`date` 是**生成时刻**，含微秒。
    # 唯一约束作用在它上面时，「一天一份」**实际没有约束住** —— 同一天不同秒即不同值。
    # 实测库里 `2026-09-15` 落了 **3 份**日报、`09-14` 与 `09-06` 各 2 份
    # （`report_service.compute_backtest` 里那段「按天去重」的注释也承认了这一点）。
    #
    # 现在把两者拆开：`date` 仍是生成时刻（**保留可追溯性**），`report_day` 是业务日；
    # 由 `db._migrate_report_day()` 建 `(report_type, report_day)` 的唯一索引。
    # 类型用 `_dt.date` 而不是裸 `date` —— 类体里已经把 `date` 这个名字绑给上面那列了。
    report_day: Mapped[_dt.date | None] = mapped_column(Date, nullable=True, index=True)
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
    # 信息量标记：True 表示当天三个信号均未触发（市场平静），报告仍生成但内容精简
    low_info: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 数据时效标记：True 表示近期新闻不足、本次研判基于陈旧数据（非当日）
    data_stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
