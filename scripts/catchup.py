"""补跑：把停机期间缺的**数据**和**报告**尽量补回来。

用法：
    .venv/Scripts/python.exe scripts/catchup.py            # 只补数据（默认）
    .venv/Scripts/python.exe scripts/catchup.py --reports  # 数据 + 报告

**补报告（--reports）会真调 LLM**（每天约 6 次：4 分析师 + 风险官 + 首席），
所以默认不跑，要显式加参数。

**报告怎么补**（`--reports`）：流程已支持 `as_of` 截止时点——
`generate_daily_report(db, date=X)` 会以 X 日 `REPORT_TIME`（默认 18:00）
为截止取**历史**数据，与当天实时生成的口径一致，两份报告可直接对比。

**只在「当天有新闻 且 有行情」时才补报告**：缺新闻的日子补出来的报告
等于凭空编造，不如没有。所以能补的通常只有最近几天（源站只保留有限新闻）。

**能补回多少，两种数据不一样**：

- **行情**：可回采 70 天，基本都能补上
- **新闻**：**不保证**。采集器受"每个源最多翻 20 页"限制，
  停得越久、新闻越多，越可能够不着。跑完请看诊断输出里仍缺的天数

两个采集都是**幂等**的（重复跑不会产生脏数据）：
- 行情按 (symbol, date) 去重，只补缺失
- 新闻按 url 去重 + 语义去重 + 多源合并

输出**只用 ASCII 标记和汉字**，不含 emoji —— Windows 中文控制台是 GBK 编码，
emoji 会让 print 直接抛 UnicodeEncodeError。
"""
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# 确保项目根目录在 sys.path，使 `from app...` 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DIAGNOSE_DAYS = 21  # 回溯多少天做缺口检查（不含今天，见 diagnose）


def _sep(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}", flush=True)


def diagnose(db) -> dict:
    """打印数据现状，并找出仍缺数据/缺报告的交易日。"""
    from app.collectors.trading_calendar import is_degraded, is_trading_day
    from app.models.market import MarketData
    from app.models.news import News
    from app.models.report import MarketReport

    news_n = db.query(News).count()
    market_n = db.query(MarketData).count()
    report_n = db.query(MarketReport).count()

    news_dates = {r[0].date() for r in db.query(News.publish_time).all() if r[0]}
    market_dates = {r[0].date() for r in db.query(MarketData.date).all() if r[0]}
    report_dates = {r[0].date() for r in db.query(MarketReport.date).all() if r[0]}

    print(f"  新闻   {news_n:5d} 条   最新 {max(news_dates) if news_dates else '—'}")
    print(f"  行情   {market_n:5d} 条   最新 {max(market_dates) if market_dates else '—'}")
    print(f"  报告   {report_n:5d} 份   最新 {max(report_dates) if report_dates else '—'}")

    today = date.today()
    # 窗口是 [今天 - DIAGNOSE_DAYS, 今天 - 1]，**刻意不含今天**：
    # 今天的行情 17:30 才采，收盘前跑会把今天误判成"缺行情"，制造假缺口。
    start = today - timedelta(days=DIAGNOSE_DAYS)
    window = [start + timedelta(days=i) for i in range(DIAGNOSE_DAYS)]
    trading_days = [d for d in window if is_trading_day(d)]

    no_market = [d for d in trading_days if d not in market_dates]
    no_report = [d for d in trading_days if d not in report_dates]
    # 新闻按「当天有没有任何新闻」判断——新闻在非交易日也会发
    no_news = [d for d in window if d not in news_dates]

    print(f"\n  近 {DIAGNOSE_DAYS} 天（不含今天）含 {len(trading_days)} 个交易日：")
    print(f"    缺行情 {len(no_market)} 天  {[str(d) for d in no_market] or '-'}")
    print(f"    缺报告 {len(no_report)} 天  {[str(d) for d in no_report] or '-'}")
    print(f"    无新闻 {len(no_news)} 天  {[str(d) for d in no_news] or '-'}")

    # 今天单独说明，避免被误读成缺口
    if is_trading_day(today):
        print(f"\n  今天 {today} 是交易日，" + (
            "行情已在。" if today in market_dates
            else "暂无行情 —— 正常，行情 17:30 才采集。"))

    # 日历降级时，上面的「交易日」判定可能是把工作日当成了交易日
    if is_degraded():
        print("\n  [!] 交易日历处于降级状态：上面的交易日判定可能不准"
              "（离线时会退化成「工作日」，法定假日可能被误算成交易日）")

    return {"no_market": no_market, "no_report": no_report, "no_news": no_news}


def backfill_reports(db, diag: dict) -> dict:
    """对「缺报告、且当天既有新闻又有行情」的交易日补生成报告。

    - 缺新闻的日子**跳过**：拿前后几天的新闻去凑一份"那天的报告"，
      等于凭空编造，比没有更糟（回测会被污染）。
    - 缺行情的日子也跳过：那种报告里没有行情快照，是残缺品。
    """
    from app.agent.graph import _report_time_on, generate_daily_report

    no_news = set(diag["no_news"])
    no_market = set(diag["no_market"])
    candidates = [
        d for d in diag["no_report"] if d not in no_news and d not in no_market
    ]
    skip_news = [d for d in diag["no_report"] if d in no_news]
    skip_market = [d for d in diag["no_report"]
                   if d in no_market and d not in no_news]

    if skip_news:
        print(f"  跳过 {len(skip_news)} 天（当天无新闻，补出来是编造）："
              f"{[str(d) for d in skip_news]}")
    if skip_market:
        print(f"  跳过 {len(skip_market)} 天（当天无行情，报告会残缺）："
              f"{[str(d) for d in skip_market]}")
    if not candidates:
        print("  没有可补报告的日子")
        return {"done": [], "failed": [], "skipped": skip_news + skip_market}

    print(f"  可补 {len(candidates)} 天：{[str(d) for d in candidates]}")
    print(f"  预计调用 LLM 约 {len(candidates) * 6} 次（4 分析师 + 风险官 + 首席）")

    done, failed = [], []
    for d in candidates:
        t = time.time()
        try:
            # 用当天的 REPORT_TIME 作时间戳——与流程内部算出的 as_of 同源，
            # 改了 REPORT_TIME 也不会出现"标签 18:00、截止却是别的点"
            rep = generate_daily_report(
                db, date=_report_time_on(datetime(d.year, d.month, d.day))
            )
            print(f"    [OK] {d}  score={rep.score}  情绪={rep.sentiment}"
                  f"  ({time.time() - t:.0f}s)", flush=True)
            done.append(str(d))
        except Exception as e:  # noqa: BLE001
            # 必须 rollback：commit 失败后 session 会进入"待回滚"状态，
            # 不清理的话后续每一天都会连锁失败
            db.rollback()
            print(f"    [X]  {d}  失败：{type(e).__name__}: {e}", flush=True)
            failed.append(str(d))
    return {"done": done, "failed": failed, "skipped": skip_news + skip_market}


def main() -> int:
    from app.db import SessionLocal

    _sep("第一步：跑之前的现状")
    db = SessionLocal()
    try:
        before = diagnose(db)

        _sep("第二步：补行情（回采 70 天，按 (symbol,date) 去重）")
        t = time.time()
        from app.services.market_service import collect_and_store_market

        n_market = collect_and_store_market(db)
        print(f"  新增 {n_market} 条，耗时 {time.time() - t:.1f}s", flush=True)

        _sep("第三步：补新闻（去重 + 分类 + 情绪打分前置，幂等）")
        print("  [!] 这一步要调 LLM，通常 10~20 分钟（取决于新闻量）", flush=True)
        t = time.time()
        from app.services.news_service import collect_and_store_news

        n_news = collect_and_store_news(db)
        print(f"  新增 {n_news} 条，耗时 {time.time() - t:.1f}s", flush=True)

        _sep("第四步：跑完之后的现状")
        after = diagnose(db)

        if "--reports" in sys.argv[1:]:
            _sep("第五步：补报告（--reports，会计费调 LLM）")
            report_result = backfill_reports(db, after)
        else:
            _sep("第五步：补报告")
            print("  已跳过（默认只补数据）。要补报告加参数：--reports")
            report_result = None

        _sep("总结")
        print(f"  本次补入：行情 {n_market} 条、新闻 {n_news} 条")
        if report_result:
            print(f"  补出报告 {len(report_result['done'])} 份，"
                  f"失败 {len(report_result['failed'])} 份")

        if after["no_market"]:
            print(f"  [!] 仍有 {len(after['no_market'])} 个交易日缺行情："
                  f"{[str(d) for d in after['no_market']]}")
            print(f"      窗口只有 {DIAGNOSE_DAYS} 天，远小于行情可回采的 70 天，")
            print("      所以这不是「超出回采范围」，而是：源站采集失败，")
            print("      或交易日历降级把非交易日误判成了交易日。")
        else:
            print(f"  [OK] 近 {DIAGNOSE_DAYS} 天（不含今天）行情无缺口")

        if after["no_news"]:
            print(f"  [i] 有 {len(after['no_news'])} 天完全没有新闻："
                  f"{[str(d) for d in after['no_news']]}")
            print("      可能是周末/节假日本来就没发，也可能是采集够不着了")
            print("      （新闻采集受「每个源最多 20 页」限制）")

        # 扣掉本次刚补出来的，否则补成功了这里还在喊"缺报告"，自相矛盾
        done = set((report_result or {}).get("done", []))
        still_no_report = [d for d in after["no_report"] if str(d) not in done]
        if still_no_report:
            print(f"  [i] 仍缺 {len(still_no_report)} 份报告："
                  f"{[str(d) for d in still_no_report]}")
            if report_result is None:
                print("      加 --reports 可补（只补当天有新闻且有行情的）")
            else:
                print("      这些日子当天无新闻或无行情，补不出来（见 未完成事项.md B5）")

        if not (before["no_market"] or before["no_news"] or before["no_report"]):
            print("  本次没有需要补的东西（近 %d 天数据本来就是齐的）" % DIAGNOSE_DAYS)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
