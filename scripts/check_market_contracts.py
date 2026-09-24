"""行情链路契约自检（2026-09-25，应第三方巡检 M5 / M6 而建）。

用法：`.venv/Scripts/python.exe scripts/check_market_contracts.py`

**为什么单独建一个**：行情链路此前**一个契约脚本都没有** ——
`check_collector_contracts.py` 覆盖的是**新闻**采集，而这次一次巡检就在行情上
找出两条（日期兜底、并发 upsert），还牵出「唯一键建在时间戳而非日期上」
这个与日报同源的毛病。没有断言的地方，改坏了也不会有人发现。

**不联网、不碰 `data/app.db`** —— 用内存 SQLite + 直接调纯函数。
"""
import ast
import inspect
import sys
import textwrap
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models  # noqa: E402,F401  触发模型注册
from app.collectors.market_collector import _to_date  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.market import MarketData  # noqa: E402
from app.services import market_service as MS  # noqa: E402

_RESULTS: list[tuple[bool, str]] = []


def check(label: str, got, want, contract: str = "") -> None:
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if contract:
        print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


def _row(symbol: str, d) -> dict:
    return {"symbol": symbol, "name": "N", "date": d, "open": 1.0, "high": 2.0,
            "low": 0.5, "close": 1.5, "volume": 10.0, "change_pct": 1.0,
            "turnover": None}


def _calls_datetime_now(fn) -> bool:
    """`fn` 的**函数体**里有没有 `.now()` 调用。**走 AST，不看注释/文档字符串。**"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "now"
        for node in ast.walk(tree)
    )


def _safe(fn, *a):
    """调被测函数，把异常变成**哨兵字符串**返回。

    ⚠️ 本项目的教训：断言若依赖「被测代码抛异常」，而脚本自己不接住，
    变异测试时看到的会是**一整条 Traceback**（后面几组全跑不到），而不是一行干净的 FAIL。
    """
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return f"<{type(e).__name__}: {e}>"


def _fresh_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def main() -> int:
    print("\n[1] 行情日期：取不到就返回 None，绝不拿当前时间冒充（巡检 M6）")
    check("`None` → None", _to_date(None), None)
    check("非日期字符串 → None", _to_date("not-a-date"), None)
    check("**空字符串 → None**", _to_date(""), None,
          "⚠️ `pd.to_datetime('')` **不抛异常**，返回 `NaT` —— 只判 `is None` 拦不住它。"
          "第一版就是这么写的，被这条断言抓出来")
    check("**`nan` → None**", _to_date(float("nan")), None,
          "同上，`pd.to_datetime(nan)` 也返回 `NaT` 而不是抛错")
    check("**`pd.NaT` → None**", _to_date(pd.NaT), None)
    check("合法日期字符串照常解析", _to_date("2026-09-24"), datetime(2026, 9, 24))
    check("带时区的 Timestamp → 去掉 tzinfo",
          _to_date(pd.Timestamp("2026-09-24 15:00", tz="Asia/Shanghai")),
          datetime(2026, 9, 24, 15, 0))
    # ⚠️ **必须用 AST，不能用源码子串匹配**。第一版写的是
    # `"datetime.now()" not in inspect.getsource(_to_date)` —— 结果**假 FAIL**：
    # `getsource` 把 **docstring** 也包含了，而 docstring 里正解释性地写着
    # 「原实现在空值 / 解析失败时 `return datetime.now()`」。
    # 「检查工具自己错了」的第五次，理由是同一个：没确认它真在检查那件事。
    check("函数体里真的没有调用 `datetime.now()`（AST 判定，不看注释）",
          _calls_datetime_now(_to_date), False,
          "凭空造一个「现在」有两个后果：① 掩盖上游异常；"
          "② 坏行的时间戳**晚于**真实行 → 在「按时间取最新」的消费者里**胜出**，"
          "静默盖掉真实当日行情（回测的 change_pct 也会被覆盖）")

    print("\n[2] 行情入库幂等：冲突在库内解决，不抛异常（巡检 M5）")
    d1, d2, d3 = (datetime(2026, 9, 24), datetime(2026, 9, 23), datetime(2026, 9, 22))
    db = _fresh_db()
    check("首次插入 2 条 → 返回 2",
          _safe(MS.save_market_data, db, [_row("sh000001", d1), _row("sh000001", d2)]), 2)
    check("**完全重复再插 → 返回 0，且不抛异常**",
          _safe(MS.save_market_data, db, [_row("sh000001", d1), _row("sh000001", d2)]), 0,
          "原实现「先查后插」在并发下两边都查不到 → 后提交者撞唯一约束 → "
          "IntegrityError 一路冒到 API 变 500，且事务已脏。"
          "改用 `ON CONFLICT DO NOTHING` 后**根本没有「查与插之间的窗口」**")
    check("部分新增 → 只计新增的那条",
          _safe(MS.save_market_data, db, [_row("sh000001", d1), _row("sh000001", d3)]), 1)
    check("库内总数 = 3（没有写重）", db.query(MarketData).count(), 3)
    check("空输入 → 0，且不发 SQL", _safe(MS.save_market_data, db, []), 0)

    print("\n[3] 行情侧互斥锁（巡检 M5）")
    check("存在进程级锁", hasattr(MS, "_MARKET_LOCK"), True,
          "新闻侧早有 `_COLLECT_LOCK`（2026-09-22 手动轮与定时轮重叠那次加的），"
          "行情侧一直是裸的")
    got = MS._MARKET_LOCK.acquire(blocking=False)
    try:
        check("持锁时采集 → **返回 0 跳过**（不排队、不联网）",
              MS.collect_and_store_market(None), 0,
              "与新闻侧同样的取舍：一轮要拉 akshare/yfinance，排队等它没有意义")
    finally:
        if got:
            MS._MARKET_LOCK.release()

    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
