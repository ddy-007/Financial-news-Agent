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


def check(label: str, got, want, contract: str) -> None:
    """一条断言。`contract` 写明它锁的是哪条契约，方便红了之后知道破坏了什么。"""
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


def _patch(levels_or_reports, next_day=None):
    """替换掉读库的两个私有函数，让判定逻辑可以脱离数据库单独跑。"""
    ev._load_reports = lambda db, limit=90: levels_or_reports
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
          "两方向都未超 0.8 时不得报警；且两分支互斥（高+低 ≤ 1）")
    check("混合分布 → 两个 ratio 并存",
          (r.get("high_ratio"), r.get("low_ratio")), (0.3, 0.3),
          "high_ratio 与 low_ratio 同时返回")

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

    # 候选池口径本身：有 veto 但 sentiment 不是「偏多」→ 一条都不该进池。
    # 注意这条**不区分新旧行为**（两边都返回 insufficient_data），
    # 它锁的是「池子怎么定义」，不是这轮改的东西。
    _patch([_report("高", veto=True, sentiment="中性") for _ in range(12)],
           next_day=lambda db, d: {"change_pct": 1.5})
    r = ev.check_risk_officer_overcaution(None)
    check("sentiment≠偏多 → 不进候选池", "无「risk_veto=True" in r.get("reason", ""),
          True,
          "候选池口径：risk_veto=True **且** sentiment=偏多，两者缺一不可")


# ============ 结构哨兵 ============
def _sentinel():
    """确认真实 `_load_reports` 的返回结构与假数据同构。

    没有这一步，本脚本会**静默失效**：若 `_load_reports` 改了返回结构，
    monkeypatch 依然"成功"，上面所有断言照过 —— 但已经跑在一条不存在的契约上。

    需要能打开数据库；打不开就 SKIP（**不判失败**：这是额外的保险，不是主检查）。
    """
    print("\n结构哨兵 _load_reports")
    required = {"date", "sentiment", "confidence", "score", "divergence",
                "risk_veto", "expert_opinions", "content"}
    db = None
    try:
        from app.db import SessionLocal
        db = SessionLocal()
        rows = ev._load_reports(db, 1)
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] 无法核对真实结构：{type(e).__name__}: {e}")
        return
    finally:
        if db is not None:
            db.close()

    if not rows:
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
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
