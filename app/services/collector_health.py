"""采集源健康监控（P3）。

**为什么必需**（设计 `新闻采集重构设计.md` §10 的 v3 补充）：财联社端点**没有备用**
（主备切换已取消，见 §8），所以「源失效」这件事**只剩下这一条发现路径** ——
没有它，财联社静默断流将无人知晓。

三条判据（任一命中即视为异常）：

    ① 陈旧     —— 距上次**完整成功** > `news_stale_factor × 采集间隔`
    ② 连续空轮  —— `empty_streak ≥ news_empty_streak_alert`
                   （源**还活着但已经取不到东西**的征兆：改版、参数失效、被限流）
    ③ 被截断    —— `truncated_at` 非空
                   （更旧的新闻本轮没取到，且**不会自动回补**）

⚠️ **陈旧告警必须能区分「源的问题」与「上游的问题」** —— 这是本模块设计上最要紧的
一点。`news_service.save_states` 里 `last_ok_at` **只在规则 3/4（推进水位线）时更新**；
规则 1（该源本轮不完整）与**规则 2（本轮有分类失败 → 所有源都不推进）**都走 `continue`。

也就是说：**LLM 连续失败几轮，`last_ok_at` 一样会变旧**。只看状态表的话，
「LLM 挂了」会被报成「源失效」，把排查引向错误方向 —— 那比不报警更糟。

所以 `evaluate()` 接受**本轮采集上下文**（`results` / `classify_failed`）：

- 在**采集轮末尾**调用（有上下文）→ `reason` 能给出准确原因；
- 由**只读接口**调用（无上下文）→ `reason` 如实标注「原因需看日志」，不装作知道。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models.collector_state import CollectorState

# 一条源可能同时命中多条判据，按此挑「主状态」（数字越小越严重）
_SEVERITY = {"never": 0, "stale": 1, "empty": 2, "truncated": 3}


@dataclass
class SourceHealth:
    source: str
    status: str                                        # never/stale/empty/truncated/ok
    flags: list[str] = field(default_factory=list)     # 命中的**全部**判据
    reason: str = ""
    last_ok_at: str | None = None
    stale_minutes: float | None = None
    empty_streak: int = 0
    truncated_at: str | None = None

    @property
    def healthy(self) -> bool:
        return self.status == "ok"


def _reason_for(source: str, *, results, classify_failed: int) -> str:
    """给出**准确**原因；没有上下文时如实说不知道，不猜。"""
    if results is None:
        return "原因需看日志（本次判定没有本轮采集上下文，无法区分是源还是上游）"
    if classify_failed > 0:
        return (f"**不是源的问题**：本轮有 {classify_failed} 条分类失败，"
                f"按规则 2 所有源的水位线都不推进，`last_ok_at` 因此未刷新")
    r = next((x for x in results if x.source == source), None)
    if r is None:
        return "本轮没有该源的采集记录（源可能已被移除或改名）"
    if not r.ok:
        return f"该源本轮采集**不完整**（失败 {r.failed_pages} 页），水位线不推进"
    if not r.items:
        return "该源本轮**成功但 0 条** —— 通常是源改版或参数失效的前兆"
    return "该源本轮正常推进了水位线"


def evaluate(db: Session, *, results=None, classify_failed: int = 0,
             now: datetime | None = None) -> dict:
    """评估各采集源的健康度，返回可 JSON 序列化的快照。

    `results` / `classify_failed` 是**本轮采集上下文**（可省略）。
    传了才能给出准确原因 —— 详见模块 docstring 里那段「区分源的问题与上游的问题」。
    """
    now = now or datetime.now()
    interval = max(1, settings.news_interval_minutes)
    stale_after = timedelta(minutes=interval * settings.news_stale_factor)

    out: list[SourceHealth] = []
    for st in db.query(CollectorState).order_by(CollectorState.source).all():
        flags: list[str] = []
        stale_minutes: float | None = None
        if st.last_ok_at is None:
            flags.append("never")
        else:
            delta = now - st.last_ok_at
            stale_minutes = round(delta.total_seconds() / 60, 1)
            if delta > stale_after:
                flags.append("stale")
        if (st.empty_streak or 0) >= settings.news_empty_streak_alert:
            flags.append("empty")
        if st.truncated_at is not None:
            flags.append("truncated")

        h = SourceHealth(
            source=st.source,
            status=min(flags, key=_SEVERITY.__getitem__) if flags else "ok",
            flags=flags,
            last_ok_at=st.last_ok_at.isoformat() if st.last_ok_at else None,
            stale_minutes=stale_minutes,
            empty_streak=st.empty_streak or 0,
            truncated_at=st.truncated_at.isoformat() if st.truncated_at else None,
        )
        if flags:
            h.reason = _reason_for(st.source, results=results,
                                   classify_failed=classify_failed)
        out.append(h)

    unhealthy = [h for h in out if not h.healthy]
    return {
        "checked_at": now.isoformat(),
        "stale_after_minutes": round(stale_after.total_seconds() / 60, 1),
        "sources": [asdict(h) for h in out],
        "unhealthy": [h.source for h in unhealthy],
        "healthy": not unhealthy,
    }


def log_health(report: dict) -> None:
    """把异常源打进日志。

    **这才是 P3 的主要交付面** —— 接口要靠人主动去查，而「断流」必须在没有人看
    的时候也能自己冒出来。采集轮末尾调用它，就等于每轮都自动巡检一次。
    """
    for h in report["sources"]:
        if h["status"] == "ok":
            continue
        logger.warning(
            f"[源健康] {h['source']} 异常（{h['status']}，命中 {h['flags']}）："
            f"{h['reason']}｜上次成功 {h['last_ok_at']}"
            f"（{h['stale_minutes']} 分钟前）｜连续空轮 {h['empty_streak']}"
            f"｜截断于 {h['truncated_at']}"
        )
    if report["healthy"] and report["sources"]:
        logger.info(f"[源健康] {len(report['sources'])} 个源均正常"
                    f"（陈旧阈值 {report['stale_after_minutes']} 分钟）")
