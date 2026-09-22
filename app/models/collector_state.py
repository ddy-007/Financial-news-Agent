"""采集水位线：每个新闻源一行，记录「上一轮成功采集到了哪个时点」。

**为什么需要它**：改动前每轮都按「距现在 N 天」重抓（`news_lookback_days`），
实测新浪/财联社 20 页只覆盖约 8 小时，而窗口是 1 天 —— 于是**每 30 分钟都撞页上限、
都在重复抓同一批**。有了水位线，下一轮只需抓「上次之后 + 一小段重叠」。

**为什么单独建表**：水位线是**采集器的运行状态**，不是新闻内容 ——
混进 `news` 表既没有承载字段，也会让"表里有什么"这件事变得含糊。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CollectorState(Base):
    __tablename__ = "collector_state"

    # 源名，与 `News.source` 同值（新浪财经 / 东方财富 / 财联社）
    source: Mapped[str] = mapped_column(String(50), primary_key=True)

    # 上轮**完整成功**采集到的源站发布时间最大值。
    # 语义是「比它更新的都已经取过了」—— 所以**采集/分类/入库任一环不完整时都不推进**，
    # 否则会永久跳过中间那段（见设计 §11 的推进规则表）。
    last_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 边界兜底：同一秒有多条时用 id 区分（当前仅东财的 realSort 用到；未用到则为 None）
    last_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 上次成功时间。P3 监控靠它判断"这个源多久没成功了"
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 连续「成功但 0 条」的轮数。持续上升通常意味着源改版或参数失效
    empty_streak: Mapped[int] = mapped_column(Integer, default=0)

    # 本轮被页上限截断的时点；未截断则置 None。
    # 它在 P3 里是一条独立告警 —— 因为截断意味着**更旧的新闻本轮没取到且不会自动回补**。
    truncated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
