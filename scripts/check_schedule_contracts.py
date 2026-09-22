"""调度契约检查 —— 新闻采集时刻**不得与任何报告时刻同刻**。

运行：`python scripts/check_schedule_contracts.py`（零依赖、不联网、不连库）

为什么要有这个检查（2026-09-22）：
    新闻采集原本硬编码 `*/30`，于是 **17:30 与行情、18:30 与周报同一分钟触发**。
    两边都要写 SQLite，而这个库**没开 WAL**、pysqlite 默认 busy timeout 只有 5 秒
    （`app/db.py` 建 engine 时只传了 `check_same_thread`）—— 撞上就是
    `database is locked`。改成 60 分钟整点后，四个报告时刻（17:30 / 17:40 /
    18:15 / 19:30）**没一个是整点**，撞车消失。

    但"整点"这件事没有任何东西挡着它回归 —— 谁把间隔改回 30、或者把周报挪回
    19:00 整点，撞车就悄悄回来了，而且**只在工作日 17:30–19:30 才发作**，平时看不出来。
    所以在这里钉成断言。

⚠️ 本脚本**必须从项目根目录运行**（下面会 `chdir` 到根目录）：
    `settings` 要读 `.env` 里真实的报告时刻。若用默认值去查，
    查的就是另一套时刻表 —— 检查工具本身反而会骗人。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)  # 见模块 docstring：必须在根目录，否则 .env 读不到
sys.path.insert(0, str(ROOT))

from apscheduler.triggers.cron import CronTrigger  # noqa: E402

from app.collectors import scheduler as S  # noqa: E402
from app.config import settings  # noqa: E402

HORIZON_DAYS = 7
# CronTrigger 返回的是 **aware** datetime（带本地时区），比较对象必须同类型。
# 用当前本地偏移构造：中国无夏令时，7 天内偏移恒定。
LOCAL_TZ = datetime.now().astimezone().tzinfo
START = datetime(2026, 9, 22, 0, 0, tzinfo=LOCAL_TZ)  # 周二，覆盖完整一周

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  —— {detail}" if detail else ""))


def fire_times(trigger: CronTrigger, days: int = HORIZON_DAYS) -> list[datetime]:
    """列出 [START, START + days) 内的全部触发时刻。"""
    end = START + timedelta(days=days)
    out: list[datetime] = []
    prev = START
    while True:
        nxt = trigger.get_next_fire_time(None, prev)
        if nxt is None or nxt >= end:
            break
        out.append(nxt)
        prev = nxt + timedelta(seconds=1)  # ⚠️ 必须前进这一秒，否则会原地返回同一时刻
    return out


def gaps_minutes(fires: list[datetime]) -> set[float]:
    """相邻两次触发的间隔（分钟）。"""
    return {(b - a).total_seconds() / 60 for a, b in zip(fires, fires[1:])}


def hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def report_hhmm() -> set[str]:
    """四个报告任务的**实际**时刻（读配置，不写死）。"""
    return {
        settings.market_collect_time,
        settings.sector_collect_time,
        settings.report_time,
        settings.weekly_report_time,
    }


def trigger_gaps(value: int):
    """构造 `value` 对应的新闻触发器并返回间隔集合；**构造本身抛异常则返回 None**。

    try 只包住**构造**那一小段：测量（`fire_times` / `gaps_minutes`）若抛异常，
    那是脚本自己的 bug，不该被伪装成"构造失败"。

    必须接住构造异常：若守卫失效，`NEWS_INTERVAL_MINUTES=1440` 会让
    `CronTrigger(hour="*/24")` 抛 `ValueError`。不接住的话脚本当场崩掉，
    后面的 [4]/[5] 一组都跑不到，看到的是一条 traceback 而不是一条 FAIL。
    """
    with interval(value):
        try:
            trig = S._news_trigger()
        except Exception as e:  # noqa: BLE001
            print(f"        · 构造 {value} 的触发器时抛 {type(e).__name__}: {e}")
            return None
    return gaps_minutes(fire_times(trig))


class interval:
    """临时改 `settings.news_interval_minutes`，退出时还原。"""

    def __init__(self, value: int) -> None:
        self.value = value

    def __enter__(self):
        self.old = settings.news_interval_minutes
        settings.news_interval_minutes = self.value
        return self

    def __exit__(self, *exc):
        settings.news_interval_minutes = self.old
        return False


def main() -> int:
    reports = report_hhmm()
    print(f"报告时刻（读自配置）：{sorted(reports)}")
    print(f"当前 NEWS_INTERVAL_MINUTES = {settings.news_interval_minutes}\n")

    # ---- ① 当前配置：不得与任何报告时刻同刻 ----
    print("[1] 当前配置与报告时刻的碰撞")
    fires = fire_times(S._news_trigger())
    hit = sorted({f.strftime("%H:%M") for f in fires} & reports)
    check(
        f"7 天内新闻触发时刻与报告时刻无交集（共 {len(fires)} 次触发）",
        not hit,
        f"撞上 {hit}",
    )
    # 节奏必须等于**配置的间隔**。真值取 `_interval_ok` 判定后的结果 ——
    # 不能写死 24 次/天（那是 60 分钟的数，换个间隔这条就误报），
    # 也不能直接信 `settings` 里的值（非法值会被回退成 60，得跟着回退）。
    effective = (
        settings.news_interval_minutes
        if S._interval_ok(settings.news_interval_minutes)
        else 60
    )
    g = gaps_minutes(fires)
    check(
        f"触发节奏恒为 {effective} 分钟（配置 {settings.news_interval_minutes}）",
        g == {float(effective)},
        f"实测 {sorted(g)}",
    )

    # ---- ② 合法间隔：节奏必须恒定 ----
    print("\n[2] 合法间隔的节奏（必须恒定，不能时快时慢）")
    for v in (15, 20, 30, 60, 120, 180, 240, 360, 480, 720):
        g = trigger_gaps(v)
        check(
            f"NEWS_INTERVAL_MINUTES={v} → 间隔恒为 {v} 分钟"
            f"（实测 {sorted(g) if g is not None else '构造失败'}）",
            g == {float(v)},
        )

    # ---- ③ 非法间隔：必须回退为 60，而不是静默漂移 ----
    #
    # 300 / 420 是 2026-09-22 巡检抓出来的：它们**是 60 的倍数**，看着合规，
    # 但 `hour="*/5"` / `"*/7"` 的间隔实测是 {240,300} / {180,420} —— 一天末尾短一截。
    # 1440 更狠：`hour="*/24"` 直接抛 ValueError，会把后端启动掀翻。
    # 这三个值必须被这条断言挡在"合法"之外。
    print("\n[3] 非法间隔的回退（含 300/420/1440 —— 60 的倍数但铺不满一天）")
    for v in (45, 90, 300, 420, 1440, 0, -5, 7, 1, 2, 5, 10, 12):
        g = trigger_gaps(v)
        check(
            f"NEWS_INTERVAL_MINUTES={v} → 回退为 60 分钟整点"
            f"（实测 {sorted(g) if g is not None else '构造失败'}）",
            g == {60.0},
        )

    # ---- ④ 周报必须给日报留出跑完的时间 ----
    #
    # 阈值 30 分钟是**判断**不是实测：日报要串四位分析师 + 风险官 + 首席，
    # 我不知道它实际跑多久（没量过）。留 30 分钟是给"明显不够"划一条下限 ——
    # 原先的 18:30 只隔 15 分钟，会在这里挂掉。
    print("\n[4] 周报与日报的间距")
    gap = hhmm(settings.weekly_report_time) - hhmm(settings.report_time)
    check(
        f"周报({settings.weekly_report_time}) 距日报({settings.report_time}) ≥ 30 分钟"
        f"（实测 {gap} 分钟）",
        gap >= 30,
        "周报可能取不到当天那份日报",
    )

    # ---- ⑤ 装配：scheduler 里**实际挂上去的**就是这个触发器 ----
    #
    # 上面几条都是直接调 `_news_trigger()`。若 `start_scheduler()` 忘了用它
    # （比如还留着旧的 `CronTrigger(minute="*/30")`），前面的检查全绿也白搭。
    # ⚠️ 只跑 `start_scheduler()` 里的 `add_job`，**不真的 `start()`**。
    # 本脚本在 docstring 里宣称"不连库"，而真把调度器拉起来的话，若恰好在整点
    # 运行，`news_collect` 会立即触发去连库、抓取、调 LLM —— 一个检查脚本
    # 不该有这个副作用。装配动作（add_job）照跑，那才是要验的东西。
    print("\n[5] 调度器装配")
    # 若调度器已在运行，`start_scheduler()` 会提前 return、news_collect 根本不会
    # 被注册，下面两条就会**误报** FAIL。先把这个前提本身说清楚。
    check("装配检查的前提：调度器未处于运行态", not S.scheduler.running)
    real_start = S.scheduler.start
    S.scheduler.start = lambda *a, **k: None  # 拦住真正的 start()
    try:
        S.start_scheduler()
        job = S.scheduler.get_job("news_collect")
        check("news_collect 已注册", job is not None)
        if job is not None:
            actual = [f.strftime("%H:%M") for f in fire_times(job.trigger)]
            expect = [f.strftime("%H:%M") for f in fire_times(S._news_trigger())]
            check(
                "news_collect 实际挂的触发器与 _news_trigger() 一致",
                actual == expect,
                f"装配的是 {actual[:4]}…，按配置应为 {expect[:4]}…",
            )
    finally:
        S.scheduler.start = real_start
        S.scheduler.remove_all_jobs()  # 清掉本次注册，别留给同进程的其它用例

    print(f"\n{'=' * 46}\n{PASS} PASS / {FAIL} FAIL\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
