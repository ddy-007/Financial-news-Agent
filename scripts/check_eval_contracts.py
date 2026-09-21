"""评估层判定契约自检：把 A3 / B2 的判定规则从「文档描述」变成「可执行断言」。

用法：`.venv/Scripts/python.exe scripts/check_eval_contracts.py`
退出码 0 = 全绿；非 0 = 有契约被改动或破坏。

**为什么需要它**：`EVAL_SPEC.md` 用文字描述 A3/B2 怎么判，代码用 if/elif 实现同一件事。
这是同一份事实的两个副本，靠人记着同步 —— 结果就是反复漂移。2026-09-21 那轮同时
暴露了三处：「永远判低」没进覆盖表、`skipped` 分支没写进文档、`up_ratio` 缺最小样本保护。
文档改不改得靠人自觉，这里把它变成**跑一下就知道**。

**它固化的是「事实」**：阈值、`status` 取值、字段存在性。
「为什么这么定」仍然只在 `EVAL_SPEC.md` —— 两者不可互相取代（片段/测试只同步事实，
对叙述性理由是盲的）。

**诚实边界**：
- 测的是**判定分支**，不测 `_load_reports` 的 SQL、不测与真实库的交互
- 用 `monkeypatch` 替换私有函数，所以若 `_load_reports` 的**返回结构**变了，
  断言会照过 —— 这正是末尾「结构哨兵」存在的原因
"""
import sys
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path，使 `from app...` 可导入（同 verify.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import evaluation as ev  # noqa: E402

# **必须在任何 _patch 之前**抓住真实函数的引用。
# 教训（2026-09-21，本脚本第一版就栽在这）：哨兵当初写的是 `ev._load_reports`，
# 而 `_b2` 最后一次 `_patch` 已把该模块属性换成了假数据函数 —— 于是哨兵核对的是
# **假数据自己**，8 个键永远齐全，missing 恒为空，只会 PASS/SKIP、永不 FAIL。
# 一个"只会通过"的保护等于没有保护，而且比没有更糟：它给出的是**虚假的安心**。
_REAL_LOAD_REPORTS = ev._load_reports
_REAL_HAS_COLUMN = ev._has_column


# ============ 假数据 ============
# 与 _load_reports 返回的 dict 同构；结构由末尾的哨兵对真实函数核对
def _report(level=None, *, veto=False, sentiment="偏多") -> dict:
    content = {}
    if level is not None:
        content["risk_opinion"] = {"risk_level": level}
    return {"date": datetime(2026, 9, 1), "sentiment": sentiment,
            "confidence": "medium", "score": 0.2, "divergence": 0.1,
            "risk_veto": veto, "expert_opinions": [], "content": content}


def _levels(*spec) -> list[dict]:
    """_levels(("高", 3), ("低", 7)) → 3 份判高 + 7 份判低的日报。"""
    return [_report(lv) for lv, n in spec for _ in range(n)]


# ============ 断言框架 ============
_RESULTS: list[tuple[bool, str]] = []
_SKIPS: list[str] = []          # 未验证项：既不算通过，也不算失败


def check(label: str, got, want, contract: str) -> None:
    """一条断言。`contract` 写明它锁的是哪条契约，方便红了之后知道破坏了什么。"""
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


def _patch(levels_or_reports, next_day=None):
    """替换掉读库的私有函数，让判定逻辑可以脱离数据库单独跑。

    `_next_day_stats` 只在显式传入时替换；B2 的非空样本用例**必须**传，
    否则会打到真实实现上拿 `db=None` 崩掉。

    ⚠️ 这里改的是**模块属性且不还原** —— 所以任何需要真实函数的地方
    （见 `_sentinel`）都得用 import 时抓好的 `_REAL_*` 引用，不能用 `ev.*`。
    """
    ev._load_reports = lambda db, limit=90: levels_or_reports
    # `_has_column` 会 inspect 真实引擎（B2 用它判断 report 表有没有 risk_veto 列）。
    # 不替换的话，缺库/缺列的环境下 B2 会走 skipped 而不是被断言的 insufficient_data，
    # 整段无谓变红 —— 本脚本不该依赖数据库是否存在。
    ev._has_column = lambda *a, **k: True
    if next_day is not None:
        ev._next_day_stats = next_day


# ============ A3 check_risk_level_distribution ============
def _a3():
    print("\nA3 check_risk_level_distribution")

    _patch(_levels(("高", 10)))
    r = ev.check_risk_level_distribution(None)
    check("全部判高 → 报「过度谨慎」", r.get("flag"),
          "风险官可能过度谨慎，降档机制趋于常态化",
          "EVAL_SPEC A3：样本≥5 且 high_ratio>0.8 → flag")
    check("全部判高 → high_ratio=1.0", r.get("high_ratio"), 1.0,
          "high_ratio = 高的占比")

    _patch(_levels(("低", 10)))
    r = ev.check_risk_level_distribution(None)
    check("A3 必须返回 low_ratio 字段", "low_ratio" in r, True,
          "2026-09-21 新增字段；缺失即契约破坏（用 in 判而非取值，"
          "缺字段要给 FAIL 而不是崩掉后面的断言）")
    check("全部判低 → 报「走过场」", r.get("flag"),
          "风险官可能退化为走过场，降档机制失去区分度",
          "EVAL_SPEC A3：样本≥5 且 low_ratio>0.8 → flag（2026-09-21 新增）")
    check("全部判低 → low_ratio=1.0", r.get("low_ratio"), 1.0,
          "low_ratio = 低的占比")

    _patch(_levels(("高", 3), ("中", 4), ("低", 3)))
    r = ev.check_risk_level_distribution(None)
    check("混合分布 → 不报 flag", r.get("flag"), None,
          "两方向都未超 0.8 时不得报警（本断言不验证两分支互斥 —— 那只在"
          "「同时超 0.8」这种数学上不可能的情形下才有意义）")
    check("混合分布 → 两个 ratio 并存",
          (r.get("high_ratio"), r.get("low_ratio")), (0.3, 0.3),
          "high_ratio 与 low_ratio 同时返回")

    # 边界：high_ratio 恰好 0.8。代码用**严格** `>`，故 0.8 不该报警。
    # 不锁这条的话，有人把 `>` 改成 `>=` 不会有任何断言拦得住。
    _patch(_levels(("高", 8), ("低", 2)))
    r = ev.check_risk_level_distribution(None)
    check("high_ratio 恰为 0.8 → 不报 flag", r.get("flag"), None,
          "阈值是严格大于：0.8 本身不触发（改成 >= 会被这条抓住）")

    # 边界：样本恰好 5。代码用 `< 5` 判不足，故 5 份应当**开始下结论**。
    _patch(_levels(("高", 5)))
    r = ev.check_risk_level_distribution(None)
    check("样本恰为 5 → ok + flag", (r.get("status"), r.get("flag")),
          ("ok", "风险官可能过度谨慎，降档机制趋于常态化"),
          "样本阈值是 <5 判不足；恰好 5 份即达线（改成 <=5 会被这条抓住）")

    _patch(_levels(("低", 3)))
    r = ev.check_risk_level_distribution(None)
    check("样本 <5 → insufficient_data", r.get("status"), "insufficient_data",
          "样本 <5 时不下结论（但仍返回分布供观察）")
    check("样本 <5 → 不报 flag", r.get("flag"), None,
          "样本不足时任何 flag 都不许出")
    check("样本 <5 → 分布仍返回", r.get("distribution"), {"高": 0, "中": 0, "低": 3},
          "insufficient_data 仍须带 distribution（供观察）")

    _patch([_report(None) for _ in range(10)])
    r = ev.check_risk_level_distribution(None)
    check("无 risk_level → skipped + reason", (r["status"], "reason" in r),
          ("skipped", True),
          "EVAL_SPEC A3：无任何报告含 risk_opinion.risk_level → skipped（须带 reason）")


# ============ B2 check_risk_officer_overcaution ============
def _b2():
    print("\nB2 check_risk_officer_overcaution")

    _patch([])
    r = ev.check_risk_officer_overcaution(None)
    check("无候选样本 → insufficient_data + reason",
          (r["status"], "reason" in r), ("insufficient_data", True),
          "无「risk_veto=True 且 sentiment=偏多」样本时须带 reason")

    _patch([_report("高", veto=True)], next_day=lambda db, d: {"change_pct": 1.5})
    r = ev.check_risk_officer_overcaution(None)
    check("n=1 且次日上涨 → 不报 flag", r.get("flag"), None,
          "最小样本保护：n<10 一律不下结论（改前会误报「系统性误伤看多判断」）")
    check("n=1 → insufficient_data", r.get("status"), "insufficient_data",
          "EVAL_SPEC B2：样本 <10 → insufficient_data（2026-09-21 新增）")
    check("n=1 → 比值仍返回", r.get("up_ratio"), 1.0,
          "样本不足时仍返回 up_ratio 供观察")

    # 边界对：9（刚好不够）与 10（刚好够）
    _patch([_report("高", veto=True) for _ in range(9)],
           next_day=lambda db, d: {"change_pct": 1.5})
    r = ev.check_risk_officer_overcaution(None)
    check("n=9 全部上涨 → 仍不下结论", r["status"], "insufficient_data",
          "最小样本阈值是 10，9 必须仍判 insufficient_data（边界下侧）")

    _patch([_report("高", veto=True) for _ in range(10)],
           next_day=lambda db, d: {"change_pct": 1.5})
    r = ev.check_risk_officer_overcaution(None)
    check("n=10 全部上涨 → ok + flag",
          (r["status"], r.get("flag")), ("ok", "可能系统性误伤看多判断"),
          "样本恰好达线即开始下结论；up_ratio=1.0 > 0.55（边界上侧）")

    # 比值高低两侧（同样 n=12）
    _patch([_report("高", veto=True) for _ in range(12)],
           next_day=lambda db, d: {"change_pct": 1.5})
    r = ev.check_risk_officer_overcaution(None)
    check("n=12 全部上涨 → 报 flag", r.get("flag"), "可能系统性误伤看多判断",
          "EVAL_SPEC B2：样本≥10 且 up_ratio>0.55 → flag")

    state = {"i": 0}

    def _half(db, d):          # 6/12 = 0.5 < 0.55
        state["i"] += 1
        return {"change_pct": 1.0 if state["i"] % 2 else -1.0}

    _patch([_report("高", veto=True) for _ in range(12)], next_day=_half)
    r = ev.check_risk_officer_overcaution(None)
    check("n=12 上涨比例 0.5 → 不报 flag", r.get("flag"), None,
          "up_ratio ≤ 0.55 不报警（严格大于才报）")

    # 边界下侧的又一个点：n=2 仍远低于 10
    _patch([_report("高", veto=True), _report("高", veto=True)],
           next_day=lambda db, d: {"change_pct": 1.5})
    r = ev.check_risk_officer_overcaution(None)
    check("n=2 全部上涨 → 仍不下结论", r.get("status"), "insufficient_data",
          "最小样本保护对任意 n<10 都生效，不只是 n=1")

    # 边界：up_ratio 恰好 0.55。代码用严格 `>`，故 0.55 不该报警。
    # 11/20 = 0.55 恰好落在阈值上。
    _b2_counter = {"i": 0}

    def _eleven_of_20(db, d):
        _b2_counter["i"] += 1
        return {"change_pct": 1.0 if _b2_counter["i"] <= 11 else -1.0}

    _patch([_report("高", veto=True) for _ in range(20)], next_day=_eleven_of_20)
    r = ev.check_risk_officer_overcaution(None)
    check("up_ratio 恰为 0.55 → 不报 flag", r.get("flag"), None,
          "阈值是严格大于：0.55 本身不触发（改成 >= 会被这条抓住）")

    # 候选池口径：**两个方向都要锁**。
    # 只证「中性被排除」是不够的 —— 若过滤条件误写成 `sentiment != "偏空"`，
    # 中性照样被排除，那条断言会照过。所以这里混合喂数据，断言**池子大小**
    # 恰好等于偏多的条数（12），多一条或少一条都算契约破坏。
    _patch([_report("高", veto=True, sentiment="偏多") for _ in range(12)]
           + [_report("高", veto=True, sentiment="中性") for _ in range(12)],
           next_day=lambda db, d: {"change_pct": 1.5})
    r = ev.check_risk_officer_overcaution(None)
    check("候选池只收 sentiment=偏多（12 偏多 + 12 中性 → 池子 12）",
          r.get("samples"), 12,
          "候选池口径：risk_veto=True **且** sentiment=偏多。"
          "断言池子大小而非「中性被排除」—— 后者拦不住误写成 !=偏空 的情形")

    # （「池子为空 → 带 reason 的 insufficient_data」已由本节第一条断言覆盖，不重复。）


# ============ 结构哨兵 ============
def _sentinel():
    """确认真实 `_load_reports` 的返回结构与假数据同构。

    没有这一步，本脚本会**静默失效**：若 `_load_reports` 改了返回结构，
    monkeypatch 依然"成功"，上面所有断言照过 —— 但已经跑在一条不存在的契约上。

    ⚠️ **必须用 `_REAL_LOAD_REPORTS`**（import 时抓的引用），不能用 `ev._load_reports`
    —— 后者此刻已被 `_patch` 换成假数据函数，核对等于自己核对自己。
    本脚本第一版就是这么错的，见文件头注释。

    需要能打开数据库；打不开就记 SKIP（**不判失败**：这是额外的保险，不是主检查，
    但不计进"通过"的分母 —— 否则会在无库环境下报一个虚高的全绿）。
    """
    print("\n结构哨兵 _load_reports")
    required = {"date", "sentiment", "confidence", "score", "divergence",
                "risk_veto", "expert_opinions", "content"}
    # 先自证：真实引用确实不是当前被 patch 的那个，否则哨兵又在自欺
    if _REAL_LOAD_REPORTS is ev._load_reports:
        _RESULTS.append((False, "结构哨兵（未生效）"))
        print("  [FAIL] 哨兵拿到的仍是已被替换的函数，核对无意义")
        return

    db = None
    try:
        from app.db import SessionLocal
        db = SessionLocal()
        rows = _REAL_LOAD_REPORTS(db, 1)
    except Exception as e:  # noqa: BLE001
        _SKIPS.append(f"结构哨兵（无法核对真实结构：{type(e).__name__}: {e}）")
        print(f"  [SKIP] 无法核对真实结构：{type(e).__name__}: {e}")
        return
    finally:
        if db is not None:
            db.close()

    if not rows:
        _SKIPS.append("结构哨兵（库中没有日报）")
        print("  [SKIP] 库里没有日报，无法核对结构")
        return
    missing = required - set(rows[0])
    _RESULTS.append((not missing, "结构哨兵"))
    if missing:
        print(f"  [FAIL] _load_reports 返回缺少键：{sorted(missing)}")
        print("         假数据已与真实结构脱节，上面的断言不再可信")
    else:
        print(f"  [PASS] 真实返回结构与假数据一致（{len(rows[0])} 个键）")


def main() -> int:
    _a3()
    _b2()
    _sentinel()
    failed = [name for ok, name in _RESULTS if not ok]
    # 跳过项**不计入分母**：把它混进「N/N 通过」会在无库环境下虚报全绿
    print(f"\n{len(_RESULTS) - len(failed)} 通过 / {len(failed)} 失败 / {len(_SKIPS)} 跳过")
    if _SKIPS:
        print("跳过项（未验证，不算通过）：")
        for s in _SKIPS:
            print(f"  - {s}")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
