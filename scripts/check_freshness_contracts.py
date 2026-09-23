"""报告新鲜度判定契约自检（H2 / H1，2026-09-23）。

用法：`.venv/Scripts/python.exe scripts/check_freshness_contracts.py`

**完全不联网、不连库** —— 测 `classify_freshness` / `is_stale` / `is_round_overdue`
这三个纯函数，外加 H1 研判侧等待的**硬上限**（[7] 会真拿一次进程内锁，约 1.5 秒）。

**为什么要单独钉它**：这个判定的失败方式是**静默的**，而且已经在 2026-09-23 真实发生过一次 ——
当天新闻 0 条，旧判据（只看「近 1 天有没有新闻」）判为**新鲜**，于是日报写出
「今日无重大消息」「未出现降准降息、LPR、财政投放等直接流动性新闻」这类**否定性结论**，
把「没看到」当成了「没发生」。

**没有留档的断言 ≈ 没有断言。**
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services.info_level import (  # noqa: E402
    classify_freshness, is_round_overdue, is_stale,
)

_RESULTS: list[tuple[bool, str]] = []


def check(label: str, got, want, contract: str = "") -> None:
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if contract:
        print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


def main() -> int:
    th = settings.info_new_threshold
    print(f"\n[1] 三档判定（阈值 info_new_threshold={th}）")

    check("当日 0 条 → error", classify_freshness(0), "error",
          "快讯是 7×24 的，当日 0 条**本身就是异常信号**。"
          "旧判据在这里返回「新鲜」—— 那正是 09-23 误读的根源")
    check(f"当日 {th - 1} 条（低于门槛）→ warn", classify_freshness(th - 1), "warn")
    check(f"当日 {th} 条（正好达门槛）→ ok", classify_freshness(th), "ok")
    check("当日条数充足 → ok", classify_freshness(999), "ok")

    print("\n[2] 采集轮仍在运行时")
    check("条数充足但采集轮在跑 → 降为 warn", classify_freshness(999, collecting=True),
          "warn",
          "此刻数据可能只是**还没到**，与「真的一条都没有」不是一回事 —— 但也不能算 ok")
    check("当日 0 条且采集轮在跑 → 仍是 error", classify_freshness(0, collecting=True),
          "error",
          "0 条就是 0 条，不会因为采集在跑就变好")

    print("\n[3] 边界与健壮性")
    check("负数（不应出现）→ 视同 0 条", classify_freshness(-1), "error")
    check("阈值可覆盖", classify_freshness(5, threshold=3), "ok")
    check("阈值覆盖后低于门槛 → warn", classify_freshness(2, threshold=3), "warn")

    print("\n[4] 落地到报告字段：`is_stale`")
    check("当日 0 条（error）→ 报告标陈旧", is_stale(False, "error"), True,
          "**这是 H2 真正落地的一步** —— 只写日志的话，打开报告的人与评估层仍然看不到。"
          "实测 09-23 那份报告 `data_stale=0`，缺失在字段上完全不可见")
    check("某类目近 1 天无新闻（原判据）→ 报告标陈旧", is_stale(True, "ok"), True)
    check("warn **不算**陈旧", is_stale(False, "warn"), False,
          "warn 表示「素材可能不全」，与「数据是旧的」不是一回事，不该占用这个字段的语义")
    check("ok → 不标陈旧", is_stale(False, "ok"), False)

    print("\n[5] ok 是唯一「可以按事实陈述」的档位")
    ok_states = [classify_freshness(n) for n in range(0, th * 2)]
    check("只有达到门槛才出现 ok", sorted(set(ok_states)), ["error", "ok", "warn"],
          "error/warn 都意味着「今天的素材不完整」，报告里的否定性结论都不该照常写")

    print("\n[6] H1 就绪判据：`is_round_overdue`（2026-09-24 新增）")
    check("上一轮刚跑完（半个间隔）→ 不算 overdue", is_round_overdue(30 * 60, 60), False)
    check("正好一个间隔 → **不算**（判据是「超过」）", is_round_overdue(60 * 60, 60), False,
          "边界取「超过」而非「达到」：整点轮次下正常时刻恰好接近一个间隔，"
          "取「达到」会把正常轮次也报成未落地")
    check("超过一个间隔 → overdue", is_round_overdue(60 * 60 + 1, 60), True,
          "整点起跑的话，账面上任何时刻都该有一轮「距今不到一个间隔」的成功记录")
    check("**从未成功过**（None）→ overdue", is_round_overdue(None, 60), True,
          "库里没有任何一轮成功记录时，按「没落地」处理才是安全侧 —— "
          "H1 的教训就是「没看到」被当成了「没发生」")
    check("判据随间隔走：30 分钟间隔下 31 分钟即 overdue",
          is_round_overdue(31 * 60, 30), True)

    print("\n[7] H1 研判侧等待：`_wait_for_collect` 的硬上限（真拿一次锁，约 1.5 秒）")
    import time as _time  # noqa: PLC0415

    from app.collectors.scheduler import _wait_for_collect  # noqa: PLC0415
    from app.services.news_service import _COLLECT_LOCK  # noqa: PLC0415

    check("没在跑 → 立即返回 True（不空等）", _wait_for_collect(5.0), True)
    _COLLECT_LOCK.acquire()
    try:
        _t0 = _time.monotonic()
        _waited_ok = _wait_for_collect(0.02)          # 预算 1.2 秒
        _elapsed = _time.monotonic() - _t0
    finally:
        _COLLECT_LOCK.release()
    check("持锁超预算 → 返回 False（**不再等下去**）", _waited_ok, False,
          "研判自己也有时效要求：无界等待等于把风险从「少用一天数据」"
          "换成「当天没有报告」—— 后者更糟")
    check("超时判定不越界（预算 1.2s，实测 < 3s）", _elapsed < 3.0, True,
          f"实测 {_elapsed:.1f}s —— 睡眠被剩余预算夹住，不会多睡一整个轮询周期")

    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
