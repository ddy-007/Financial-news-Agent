"""补跑：把停机期间缺的**数据**尽量补回来。

用法：
    .venv/Scripts/python.exe scripts/catchup.py

**只补数据（新闻 + 行情），不生成报告。**

为什么报告不补：当前流程把"现在"写死（`graph.py` 的 `prepare_node` 用
`datetime.now()` 取数据），`generate_daily_report(db, date=...)` 的 `date`
只用来写报告上的日期标签。用旧日期调用会得到"标签写 A 日、内容却是今天"
的报告 —— 回测时等于用未来信息预测过去，比不补更糟。

补报告需要先让 `as_of` 日期穿透整条流程，见 `未完成事项.md` 的 B5。

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
from datetime import date, timedelta
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

        _sep("总结")
        print(f"  本次补入：行情 {n_market} 条、新闻 {n_news} 条")

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

        if after["no_report"]:
            print(f"  [i] 有 {len(after['no_report'])} 个交易日缺报告："
                  f"{[str(d) for d in after['no_report']]}")
            print("      报告**无法事后补**（会让回测失真），见 未完成事项.md B5")

        if not (before["no_market"] or before["no_news"] or before["no_report"]):
            print("  本次没有需要补的东西（近 %d 天数据本来就是齐的）" % DIAGNOSE_DAYS)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
