"""API / 工具 / 前端边界契约自检（2026-09-25，应第三方巡检 M1/M2/M7/L1 而建）。

用法：`.venv/Scripts/python.exe scripts/check_api_contracts.py`

**为什么不启服务**：全部用**纯函数 + 签名内省 + 源码文本**来钉，不建 TestClient ——
`TestClient` 会跑 lifespan，那会**真的启动调度器**并连库，测试不该有这种副作用。

**它钉住的是「静默失败」那一类**：
  · 前端对 `score=None` / 非 dict 的 content 会**整页崩**；
  · 灰区判重把 `not true` 当成「重复」，会**永久合并掉真实新闻**（不可逆）；
  · `limit=-1` 在 SQLite 里等于**不限行数**（实测返回全表 9543 行）。
这几种都不会报错、不会告警，只会在某天悄悄发生 —— 所以要有断言，而不是靠人记得。
"""
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.data_agent import _normalize_verdict  # noqa: E402
from app.agent.tools import _clamp_limit  # noqa: E402
from app.api.routes_market import list_market  # noqa: E402
from app.api.routes_news import list_news  # noqa: E402
from app.api.routes_reports import list_reports  # noqa: E402
from app.models.news import News  # noqa: E402
from app.models.report import MarketReport  # noqa: E402
from app.services.report_service import compute_backtest, report_to_dict  # noqa: E402
from app.config import settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_RESULTS: list[tuple[bool, str]] = []


def check(label: str, got, want, contract: str = "") -> None:
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if contract:
        print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


def _bounds(fn, name: str) -> tuple:
    """取路由函数某参数的 `Query(ge=, le=)` 边界。

    ⚠️ **不能读 `.ge` / `.le` 属性**（第一版就是这么写的，4 条断言全部**假 FAIL**）：
    FastAPI 0.141 + Pydantic 2.13 把约束放进了 `metadata` ——
    `[Ge(ge=1), Le(le=1000)]`，属性上取不到。
    **又一次「检查工具自己错了」**：报 FAIL 之前先确认它真在检查那件事。
    """
    d = inspect.signature(fn).parameters[name].default
    lo = hi = None
    for m in getattr(d, "metadata", None) or []:
        if hasattr(m, "ge"):
            lo = m.ge
        if hasattr(m, "le"):
            hi = m.le
    return lo, hi


def main() -> int:
    print("\n[1] 灰区判重：只认精确 true / false（巡检 L1）")
    check("`true` → True", _normalize_verdict("true"), True)
    check("`TRUE` → True（大小写不敏感）", _normalize_verdict("TRUE"), True)
    check("带空白 / 句号 → True", _normalize_verdict("  true. \n"), True)
    check("``` 围栏包裹 → True", _normalize_verdict("```\ntrue\n```"), True)
    check("`false` → False", _normalize_verdict("false"), False)
    check("**`not true` → None**（不是 True！）", _normalize_verdict("not true"), None,
          "原实现 `\"true\" in resp.lower()` 把它判成「重复」—— **语义恰好相反**，"
          "会把真实新闻永久合并掉")
    check("**`untrue` → None**", _normalize_verdict("untrue"), None, "子串命中")
    check("**`true. 因为两者都是…` → None**", _normalize_verdict("true. 因为两者都是同一事件"), None,
          "模型没严格守「只输出 true/false」时，带解释的响应不该被当作确认")
    check("空 / None → None", (_normalize_verdict(""), _normalize_verdict(None)), (None, None))
    check("拿不准**一律 None**，交给上层 fail-open 当新条目",
          _normalize_verdict("maybe"), None,
          "误合并不可逆，多存一条可逆 —— 代价不对称，所以拿不准就不合")

    print("\n[2] 报告 content 必须恒为 dict（巡检 M1）")
    mk = lambda body: report_to_dict(  # noqa: E731
        MarketReport(date=__import__("datetime").datetime.now(), title="t", content=body))
    check("正常 JSON 对象 → 原样返回",
          mk('{"market_summary": "x"}')["content"], {"market_summary": "x"})
    check("**非 JSON 文本（历史 markdown）→ 包成 dict**",
          isinstance(mk("legacy markdown")["content"], dict), True,
          "原实现回退成 `str`，前端 `c.get(...)` 会抛 AttributeError，**整页崩**")
    check("原文没有丢，放在 `raw` 里", mk("legacy markdown")["content"], {"raw": "legacy markdown"})
    check("**合法 JSON 但顶层是数组 → 也包成 dict**",
          isinstance(mk("[1, 2, 3]")["content"], dict), True,
          "这是巡检报告**漏掉的那一支**：list 同样没有 `.get`，一样崩")
    check("空内容 → 空 dict", mk("")["content"], {})

    print("\n[3] 查询参数边界（巡检 M7）")
    check("news `limit` 夹在 [1, 1000]", _bounds(list_news, "limit"), (1, 1000),
          "`limit=-1` 在 SQLite 里是「**不限制**」，实测返回全表 9543 行。"
          "上限 1000 = 前端 selectbox 的最大档")
    check("news `days` 夹在 [1, 30]", _bounds(list_news, "days"), (1, 30),
          "`days=0` 是 falsy、被静默当成「未传」，与「今天」的直觉相反")
    check("news `offset` 不允许负数", _bounds(list_news, "offset"), (0, None))
    check("market `limit` 夹在 [1, 1000]", _bounds(list_market, "limit"), (1, 1000))
    check("reports `limit` 夹在 [1, 200]", _bounds(list_reports, "limit"), (1, 200))

    print("\n[4] Agent 工具的硬上限（LLM 传的参数不可信）")
    check("正常值原样通过", _clamp_limit(10), 10)
    check("**`-1` → 1**（不是不限行数）", _clamp_limit(-1), 1)
    check("超大值被夹到上限", _clamp_limit(999999), 50)
    check("非整数 → 回调退值", _clamp_limit("abc"), 10)
    check("自定义 fallback", _clamp_limit(None, fallback=5), 5)

    print("\n[5] 前端源码守卫（巡检 M2）")
    src = (ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
    bad = re.findall(r"\.get\([^)]*\)\s*:\+\.2f", src)
    check("不再有 `xxx.get(...):+.2f` 这类写法", bad, [],
          "键存在但值为 None 时 `.get(k, 0)` 的默认值**不生效**，"
          "`f\"{None:+.2f}\"` 直接抛 TypeError。已统一走 `_fmt_score`")
    check("`_fmt_score` 对非数值返回占位符而不是 0",
          'return f"{v:+.2f}" if isinstance(v, (int, float)) else "—"' in src, True,
          "兜成 0 是错的方向：0 是「中性」这个**真实结论**，None 是「拿不到」，"
          "把后者显示成 +0.00 等于凭空造一个结论")
    check("两处分数都改用了 `_fmt_score`",
          src.count("_fmt_score(data.get(\"score\"))"), 2)

    print("\n[6] 就绪探针的三档判定（巡检 M8）")
    from app.main import _readiness_status, health  # noqa: PLC0415

    check("存活探针仍是不带依赖的常量（不能改成查库）",
          health(), {"status": "ok"},
          "把依赖检查塞进存活探针会变成「进程没死但探针失败 → 被反复重启」")
    ok_all = {"database": True, "bm25_ready": True, "scheduler_running": True}
    check("全好 → ok", _readiness_status(ok_all), "ok")
    check("**数据库挂了 → unavailable**（唯一给 503 的情形）",
          _readiness_status({**ok_all, "database": "OperationalError: x"}), "unavailable",
          "数据库是唯一的硬依赖 —— 它挂了任何接口都做不了事")
    check("BM25 空 → degraded（**不是 503**）",
          _readiness_status({**ok_all, "bm25_ready": False}), "degraded",
          "BM25 为空只是检索质量降级（退化为纯向量），服务照常可用。"
          "算成 503 会让负载均衡在**服务其实能用**时把它摘掉 —— 比不报更糟")
    check("调度器没跑 → degraded",
          _readiness_status({**ok_all, "scheduler_running": False}), "degraded",
          "只是「不再自动采集」，手动接口仍然工作")
    check("数据库缺失（键都没有）→ unavailable",
          _readiness_status({}), "unavailable")

    print("\n[7] 日报「一天一份」真的生效了（巡检 H2）")
    import datetime as _dt  # noqa: PLC0415

    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    import app.models  # noqa: PLC0415,F401
    from app.models.base import Base  # noqa: PLC0415
    from app.services.report_service import upsert_daily_report  # noqa: PLC0415

    _eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(_eng)
    _db = sessionmaker(bind=_eng)()

    for i in range(4):
        _db.add(News(title=f"分页新闻 {i}", source="测试", content="",
                     url=f"https://example.com/news/{i}",
                     publish_time=_dt.datetime(2026, 9, 24, 12, 0)))
    _db.commit()

    def _news_page(limit, offset):
        return [r["id"] for r in list_news(
            days=None, start=None, end=None, limit=limit, offset=offset, db=_db
        )]

    check("同一发布时间的新闻翻页不重不漏",
          _news_page(2, 0) + _news_page(2, 2), _news_page(4, 0))

    def _mk(dayh, **over):
        f = {"date": dayh, "title": "t", "content": "{}", "score": 0.1}
        f.update(over)
        return f

    # ⚠️ 必须接住异常：这些断言依赖「同日覆盖」，而被测代码一旦退化成「无条件插入」，
    # 就会撞唯一索引抛 IntegrityError —— 第一版没接住，变异时看到的是**一整条
    # Traceback**（后面全跑不到），而不是一行 FAIL。本项目栽过同一个坑。
    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            return f"<{type(e).__name__}: {e}>"

    d1 = _dt.datetime(2026, 9, 24, 18, 30, 1, 111)
    r1 = _safe(upsert_daily_report, _db, d1.date(), **_mk(d1))
    n1 = _db.query(MarketReport).count()
    d1b = _dt.datetime(2026, 9, 24, 19, 5, 22, 222)      # 同一天、晚 35 分钟
    r2 = _safe(upsert_daily_report, _db, d1b.date(), **_mk(d1b, score=0.9))
    _db.rollback()   # 万一撞了约束，事务是脏的 —— 后面的断言需要一个能用的会话

    # 断言一律**不假设 r1/r2 是对象**：被测代码一退化成「无条件插入」，
    # `_safe` 返回的就是哨兵字符串，此时下面几条会给出干净的 FAIL，
    # 而不是先炸在属性访问上（第一版就是这样，变异时看到的是整条 Traceback）。
    check("同一天第二次写入 → **原地覆盖，不新增**",
          (not isinstance(r2, str),
           _db.query(MarketReport).count(),
           getattr(r2, "id", None) == getattr(r1, "id", None)),
          (True, n1, True),
          "改动前是无条件 `db.add`，同日判定落在含微秒的 `date` 上 —— 实测库里 "
          "2026-09-15 落了 **3 份**、09-14/09-06 各 2 份，而且**不报任何错**")
    check("覆盖后 score 更新为新值（不是留旧的）", getattr(r2, "score", r2), 0.9)
    d2 = _dt.datetime(2026, 9, 25, 18, 30, 0, 0)
    upsert_daily_report(_db, d2.date(), **_mk(d2))
    check("换一天 → 正常新增一份", _db.query(MarketReport).count(), n1 + 1)
    # 同一天再来一份**周报**（不同 report_type）→ 应当允许
    _db.add(MarketReport(date=d1, report_type="weekly", report_day=d1.date(),
                         title="同日的周报", content="{}"))
    _db.commit()
    check("日报与周报互不干扰（唯一键含 report_type）",
          _db.query(MarketReport).count(), n1 + 2)

    # 身份键不许经 fields 传 —— 传了会 `setattr` 盖掉已有行的 report_type，
    # 而 SQLite 的唯一索引**认为 NULL 互不相等**，「一天一份」当场失效。
    # 同样走 `_safe`：不加护栏时这里会 `setattr` 盖掉已有行的身份键，
    # 进而撞上**别的**唯一约束（实测撞的是 `report.date`）——
    # 不接住就是又一条 Traceback。
    _r = _safe(upsert_daily_report, _db, d1.date(), **_mk(d1, report_type="weekly"))
    _db.rollback()
    check("`report_type` 经 fields 传入 → 必须拒绝",
          str(_r).startswith("<ValueError"), True,
          "实测就是这么发现隐患的：传一次 `report_type=None` 把已有日报改了类型，"
          "而 SQLite 的唯一索引**认为 NULL 互不相等**，「一天一份」当场失效")

    # 库级约束是最后一道防线：绕过应用层直接插同日重复，**必须被拒**
    # ⚠️ `date` 必须**错开**（与已有行不同的一秒）。
    # 第一版直接复用了 `d1b` —— 于是这次插入是被 **`date` 上的唯一索引**
    # （`ix_report_date`，早就存在）拒掉的，跟 `(report_type, report_day)` 毫无关系：
    # 断言在**为错误的理由通过**。是变异测试（把模型里的 Index 摘掉却不 FAIL）抓出来的。
    _raw = MarketReport(date=d1b + _dt.timedelta(seconds=1), report_type="daily",
                        report_day=d1b.date(),
                        title="绕过应用层的写入", content="{}")
    _db.add(_raw)
    try:
        _db.commit()
        check("直接插同日重复 → 被唯一索引拒绝", "竟然成功了", "IntegrityError")
    except IntegrityError:
        check("直接插同日重复 → 被唯一索引拒绝", "IntegrityError", "IntegrityError",
              "应用层的 upsert 是**软**保护（单进程），库级唯一索引才是硬约束。"
              "两处都要有：全新库靠模型声明，既有库靠 `_migrate` 补建")
    finally:
        _db.rollback()

    check("`report_to_dict` 暴露了 report_day（前端按天分组要用它）",
          "report_day" in report_to_dict(r1), True)
    check("回测响应暴露当前中性带（前端规则说明与后端同源）",
          compute_backtest(_db)["score_neutral_band"],
          settings.score_neutral_band)

    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
