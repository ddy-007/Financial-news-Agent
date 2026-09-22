"""采集源健康监控（P3）的判定契约自检。

用法：`.venv/Scripts/python.exe scripts/check_health_contracts.py`
退出码 0 = 全绿；非 0 = 有契约被改动或破坏。

**完全不联网、不调 LLM、不碰 `data/app.db`** —— 内存 SQLite + 构造的 `collector_state` 行。

为什么值得钉成断言（设计 `新闻采集重构设计.md` §10）：

    财联社端点**没有备用**（主备切换已取消），所以「源失效」只剩下 P3 这一条发现路径。
    P3 自己错了，就等于没有发现路径 —— 而且是**静默**的没有。

⚠️ 本脚本最要紧的三条断言，测的都是「告警能不能把排查引到对的地方」：

    · 有 `classify_failed` 上下文时，原因必须写明「**不是源的问题**」；
    · 该源 `ok=False` 时，原因必须写明是**采集不完整**；
    · 没有上下文时，原因必须如实说「需看日志」，**不许装作知道**。

    因为 `save_states` 里 `last_ok_at` 只在规则 3/4 更新 —— LLM 连续失败几轮
    同样会把它拖旧。只按状态表判，会把「LLM 挂了」报成「源失效」。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models  # noqa: E402,F401  触发模型注册
from app.collectors.news_collector import NewsItem, SourceResult  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.collector_state import CollectorState  # noqa: E402
from app.services.collector_health import evaluate, log_health  # noqa: E402

_RESULTS: list[tuple[bool, str]] = []
NOW = datetime(2026, 9, 22, 12, 0, 0)


def check(label: str, got, want, contract: str = "") -> None:
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if contract:
        print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


def fresh_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def state(db, source="财联社", **kw):
    st = CollectorState(source=source, empty_streak=kw.pop("empty_streak", 0), **kw)
    db.add(st)
    db.commit()
    return st


def sr(source="财联社", n=1, ok=True, failed=0, truncated=False) -> SourceResult:
    items = [NewsItem(title=f"T{n}", source=source, publish_time=NOW)] if n else []
    return SourceResult(source=source, items=items, rejected=[], ok=ok,
                        failed_pages=failed, truncated=truncated)


class interval:
    """临时改 `settings.news_interval_minutes`，退出时还原。"""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = settings.news_interval_minutes
        settings.news_interval_minutes = self.value

    def __exit__(self, *exc):
        settings.news_interval_minutes = self.old
        return False


# ============ 三条判据 ============
def _judgements():
    print("\n[1] 三条判据与边界")

    db = fresh_db()
    st = state(db, last_ok_at=None)
    h = evaluate(db, now=NOW)["sources"][0]
    check("从未成功 → status=never", (h["status"], h["flags"]), ("never", ["never"]))

    # 阈值 = 3 × 60 = 180 分钟
    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(minutes=179))
    check("距上次成功 179 分钟（阈值 180）→ 正常",
          evaluate(db, now=NOW)["sources"][0]["status"], "ok")

    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(minutes=181))
    check("距上次成功 181 分钟 → stale",
          evaluate(db, now=NOW)["sources"][0]["status"], "stale")

    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(minutes=180))
    check("**正好等于阈值 → 正常**（判据是严格大于，不是 ≥）",
          evaluate(db, now=NOW)["sources"][0]["status"], "ok",
          "边界写成 ≥ 会让重启后的第一轮就误报")

    db = fresh_db()
    state(db, last_ok_at=NOW, empty_streak=settings.news_empty_streak_alert)
    check("连续空轮达阈值 → empty",
          evaluate(db, now=NOW)["sources"][0]["status"], "empty")

    db = fresh_db()
    state(db, last_ok_at=NOW, empty_streak=settings.news_empty_streak_alert - 1)
    check("连续空轮差一轮 → 正常",
          evaluate(db, now=NOW)["sources"][0]["status"], "ok")

    db = fresh_db()
    state(db, last_ok_at=NOW, truncated_at=NOW - timedelta(minutes=5))
    check("被页上限截断 → truncated",
          evaluate(db, now=NOW)["sources"][0]["status"], "truncated")


def _severity():
    print("\n[2] 多判据同时命中时取最严重")

    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(days=1),
          empty_streak=99, truncated_at=NOW)
    h = evaluate(db, now=NOW)["sources"][0]
    check("stale/empty/truncated 全命中 → 取最严重的 stale", h["status"], "stale")
    check("但 flags 保留**全部**命中项（排查时都要知道）",
          sorted(h["flags"]), ["empty", "stale", "truncated"])

    db = fresh_db()
    state(db, last_ok_at=NOW, empty_streak=99, truncated_at=NOW)
    check("empty 与 truncated 同时命中 → empty 更严重",
          evaluate(db, now=NOW)["sources"][0]["status"], "empty")


def _reason():
    print("\n[3] 原因必须把排查引向对的地方 ⭐")

    # ⭐ 核心：LLM 故障导致水位线不推进，绝不能报成「源失效」
    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(hours=6))
    h = evaluate(db, results=[sr(ok=True)], classify_failed=1120, now=NOW)["sources"][0]
    check("有 classify_failed 上下文 → 原因写明「不是源的问题」",
          "不是源的问题" in h["reason"], True,
          "save_states 里 last_ok_at 只在规则 3/4 更新 —— 规则 2（分类失败）"
          "同样会把它拖旧。只按状态表判会把「LLM 挂了」报成「源失效」")

    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(hours=6))
    h = evaluate(db, results=[sr(ok=False, failed=3)], classify_failed=0,
                 now=NOW)["sources"][0]
    check("有该源不完整的上下文 → 原因写明采集不完整",
          "不完整" in h["reason"] and "3 页" in h["reason"], True)

    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(hours=6))
    h = evaluate(db, results=[sr(n=0)], classify_failed=0, now=NOW)["sources"][0]
    check("有该源 0 条的上下文 → 原因写明「成功但 0 条」",
          "0 条" in h["reason"], True)

    # 没有上下文时，如实说不知道
    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(hours=6))
    h = evaluate(db, now=NOW)["sources"][0]
    check("**无上下文 → 原因如实说「需看日志」，不装作知道**",
          "原因需看日志" in h["reason"], True,
          "只读接口正是这条路径 —— 它区分不了源故障与上游故障，就不该猜")

    # 正常的源不该有 reason（避免日志噪音）
    db = fresh_db()
    state(db, last_ok_at=NOW)
    check("正常的源不带 reason", evaluate(db, now=NOW)["sources"][0]["reason"], "")


def _threshold_scaling():
    print("\n[4] 阈值随采集间隔缩放")

    db = fresh_db()
    state(db, last_ok_at=NOW - timedelta(minutes=100))
    with interval(60):
        check("间隔 60 → 阈值 180 分钟（100 分钟前仍算正常）",
              evaluate(db, now=NOW)["stale_after_minutes"], 180.0)
    with interval(30):
        # 间隔缩到一半，同样的陈旧度就该告警了
        check("间隔 30 → 阈值 90 分钟（100 分钟前已算陈旧）",
              evaluate(db, now=NOW)["sources"][0]["status"], "stale",
              "阈值是「倍数 × 间隔」—— 调采集频率时不用手改阈值，但判定要跟着变")


def _summary():
    print("\n[5] 汇总与日志落点")

    db = fresh_db()
    check("空表（还没跑过采集）→ 视为健康，不虚报", evaluate(db, now=NOW)["healthy"], True)

    db = fresh_db()
    state(db, source="新浪财经", last_ok_at=NOW)
    state(db, source="东方财富", last_ok_at=NOW)
    state(db, source="财联社", last_ok_at=NOW - timedelta(days=2))
    r = evaluate(db, now=NOW)
    check("unhealthy 只列异常源", r["unhealthy"], ["财联社"])
    check("healthy 为 False", r["healthy"], False)

    # 告警必须真的进日志 —— 接口要靠人主动查，断流必须能自己冒出来
    class Sink:
        def __init__(self):
            self.lines = []

        def write(self, msg):
            self.lines.append(str(msg))

    sink = Sink()
    hid = logger.add(sink.write, level="WARNING", format="{message}")
    try:
        log_health(r)
    finally:
        logger.remove(hid)
    joined = "".join(sink.lines)
    check("异常源被打进 WARNING 日志（含源名与原因）",
          "财联社" in joined and "原因需看日志" in joined, True,
          "这是 P3 的主要交付面 —— 接口要靠人主动查，断流必须能自己冒出来")

    sink2 = Sink()
    hid = logger.add(sink2.write, level="INFO", format="{message}")
    try:
        log_health({"sources": [], "healthy": True, "unhealthy": [],
                    "stale_after_minutes": 180.0, "checked_at": ""})
    finally:
        logger.remove(hid)
    check("一条源都没有时不打日志（避免空跑噪音）", sink2.lines, [])


def main() -> int:
    _judgements()
    _severity()
    _reason()
    _threshold_scaling()
    _summary()
    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
