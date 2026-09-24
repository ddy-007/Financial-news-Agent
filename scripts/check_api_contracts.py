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
from app.models.report import MarketReport  # noqa: E402
from app.services.report_service import report_to_dict  # noqa: E402

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
          "`days=0` 是 falsy、被静默当成「未传」，与「今天」的直觉相反；"
          "1~30 与前端滑块对齐")
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

    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
