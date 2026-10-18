"""评估层判定契约自检：S5 / A2 / A3 / B2 / S8。

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
- S5/A2 使用内存 SQLite 验证 SQL 与判定；A3/B2/S8 用假数据验证分支
- 用 `monkeypatch` 替换私有函数，所以若 `_load_reports` 的**返回结构**变了，
  断言会照过 —— 这正是末尾「结构哨兵」存在的原因
"""
import sys
import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

# 确保项目根目录在 sys.path，使 `from app...` 可导入（同 verify.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import experts as ex  # noqa: E402
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
def _report(level=None, *, veto=False, sentiment="偏多", opinions=None) -> dict:
    content = {}
    if level is not None:
        content["risk_opinion"] = {"risk_level": level}
    return {"date": datetime(2026, 9, 1), "sentiment": sentiment,
            "confidence": "medium", "score": 0.2, "divergence": 0.1,
            "risk_veto": veto, "expert_opinions": opinions or [], "content": content}


def _opinion(expert: str, stance: str, score: float) -> dict:
    return {"expert": expert, "stance": stance, "score": score}


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


# ============ S8 check_stance_consistency ============
def _s8():
    print("\nS8 check_stance_consistency")

    _patch([_report(opinions=[
        _opinion("宏观", "看多", 0.35), _opinion("行业", "看空", -0.40),
        _opinion("资金面", "中性", 0.00),
    ])])
    r = ev.check_stance_consistency(None)
    check("全部自洽 → 0 条矛盾", (r["mismatches"], r["mismatch_rate"]), (0, 0.0),
          "score 落在对应档内时不计矛盾")

    _patch([_report(opinions=[
        _opinion("行业", "中性", 0.32), _opinion("宏观", "看多", 0.35),
    ])])
    r = ev.check_stance_consistency(None)
    check("明确偏多却写中性 → 记 1 条矛盾",
          (r["total"], r["mismatches"], r["samples"][0]["expected"]),
          (2, 1, "看多"),
          "score 明确落在非中性档内部时，stance 必须对应")

    # ★ 这条是踩过坑才加的：锚点里 ±0.15 被「中性」和「偏多/偏空」两个区间同时包含。
    # 我第一次统计时把 3 条边界样本误算成矛盾（12.5%），排除后真矛盾只有 1/32。
    _patch([_report(opinions=[
        _opinion("资金面", "中性", 0.15), _opinion("资金面", "中性", -0.15),
        _opinion("宏观", "看多", 0.15), _opinion("宏观", "看空", -0.15),
    ])])
    r = ev.check_stance_consistency(None)
    check("±0.15 边界**不算**矛盾", (r["total"], r["mismatches"]), (4, 0),
          "锚点的 ±0.15 被两个区间同时包含，两种写法都对 —— 这条若变红，"
          "说明边界处理被改坏了，会把边界样本误报成矛盾")

    _patch([_report()])           # expert_opinions 为空
    r = ev.check_stance_consistency(None)
    check("无 expert_opinions → skipped + reason",
          (r["status"], "reason" in r), ("skipped", True),
          "无观点可查时不下结论，也不除零")

    _patch([_report(opinions=[_opinion("行业", "中性", 0.32)] * 5)])
    r = ev.check_stance_consistency(None)
    check("矛盾 ≥5 条 → 出 flag", r.get("flag"),
          "立场与分值频繁矛盾 —— 考虑改为「由 score 反推 stance」",
          "样本够 5 条才提示；低于 5 只报数不报警")

    # 这个常量曾在 experts.py 与 evaluation.py 各写一遍（巡检标「中」）。
    # 现在 evaluation 从 experts import —— 牢牢钉住"只有一个来源"。
    check("STANCE_NEUTRAL_EDGE 只有一个来源",
          ev.STANCE_NEUTRAL_EDGE is ex.STANCE_NEUTRAL_EDGE, True,
          "两处各写一遍 0.15，改一处会让写入侧告警与 S8 统计静默不一致")

    # 脏数据：库里的 expert_opinions 是 JSON，元素可能不是 dict、score 可能是串或 null。
    # 一条脏记录不该让整条 S8 变成 error（那样全部统计都丢了）。
    _patch([_report(opinions=[
        _opinion("宏观", "看多", 0.35),              # 有效
        {"expert": "行业", "stance": "中性", "score": "abc"},   # 非数值
        {"expert": "资金面", "stance": "中性", "score": None},  # 缺失
        "not-a-dict",                                          # 元素不是 dict
    ])])
    r = ev.check_stance_consistency(None)
    check("脏数据不崩，且只统计合法条目",
          (r["status"], r["total"], r["mismatches"]), ("ok", 1, 0),
          "逐条挡脏值；否则一条脏记录就让整条 S8 变 error，统计全丢")


# ============ 结构哨兵 ============
def _sqlite_path(url: str) -> Path | None:
    """从 SQLAlchemy 的 sqlite URL 取出本地文件路径；不是 sqlite 就返回 None。"""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    return Path(url[len(prefix):]).resolve()


def _s5_a2():
    """真实 SQL 走内存库；不调用模型、不联网、不写业务库。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models.base import Base
    from app.models.market import MarketData
    from app.models.report import MarketReport

    print("\nS5/A2 分析时点、缺失数据及词频复核")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db, patch.object(ev, "_load_reports", _REAL_LOAD_REPORTS):
        report = MarketReport(date=datetime(2026, 10, 15, 18, 30), report_day=date(2026, 10, 15),
                              report_type="daily", title="测试", sentiment="偏空",
                              content=json.dumps({"market_summary": "上证指数-1.59%。"}))
        db.add_all([report,
                    MarketData(symbol="sh000001", date=datetime(2026, 10, 15),
                               close=3800, change_pct=-1.59),
                    MarketData(symbol="sh000001", date=datetime(2026, 10, 16),
                               close=3900, change_pct=0.24),
                    MarketData(symbol=".INX", date=datetime(2026, 10, 14),
                               close=6000, change_pct=-1.11),
                    MarketData(symbol=".INX", date=datetime(2026, 10, 15),
                               close=6100, change_pct=0.33)])
        db.commit()

        def evaluate(text, *, freshness=None):
            content = {"market_summary": text}
            if freshness is not None:
                content["data_freshness"] = freshness
            report.content = json.dumps(content, ensure_ascii=False)
            db.commit()
            return ev.check_fact_consistency(db)

        r = evaluate("上证指数-1.59%。")
        check("历史日报用历史行情", (r["matched"], r["numbers_found"]), (1, 1),
              "不能拿全库最新行情比较旧日报")
        check("旧报告注明时间回退", r["comparisons"][0]["cutoff_basis"], "legacy_report_date",
              "旧报告没有输入快照，不能声称精确复原")
        baseline = r
        db.add(MarketData(symbol="sh000001", date=datetime(2026, 10, 19), change_pct=9))
        db.commit()
        check("新增未来行情不改变历史结果", evaluate("上证指数-1.59%。"), baseline,
              "未来行情不得污染历史校验")
        r = evaluate("标普500下跌1.11%。")
        check("美股使用中国分析时点之前已收盘的交易日", r["matched"], 1,
              "10/15 18:30中国时间不能使用10/15美股收盘")
        check("美股实际日期可追溯", r["comparisons"][0]["market_date"][:10], "2026-10-14",
              "A股与美股不能强制同一自然日")
        db.add(MarketData(symbol=".INX", date=datetime(2026, 1, 5), close=5000, change_pct=1))
        db.commit()
        for hour, available in ((4, False), (5, True)):
            snapshot = ev._latest_market_map(db, as_of=datetime(2026, 1, 6, hour, 30))
            check(f"美股冬令时收盘边界 {hour}:30", ".INX" in snapshot, available,
                  "纽约冬令时16:00对应北京时间次日05:00，不能固定偏移")
        r = evaluate("上证指数+0.24%。", freshness={"analyzed_at": "2026-10-15T18:30:00"})
        check("分析时点优先于后来行情", len(r["mismatched"]), 1,
              "不能用未来行情中相同的值放行历史报告")
        r = evaluate("上证指数-1.59%。", freshness={"analyzed_at": "2026-10-15T13:00:00"})
        check("盘中不得用当天收盘日线", (r["status"], r["numbers_found"], r["match_rate"]),
              ("skipped", 0, None), "日线日期零点不代表收盘数据零点已可用")
        for raw in ("bad", 4, "2026-10-16T18:30:00"):
            r = evaluate("上证指数-1.59%。", freshness={"analyzed_at": raw})
            check(f"坏时点或业务日冲突 {raw!r} 不算通过", (r["status"], r["matched"]),
                  ("skipped", 0), "无法还原的时点不能静默回退当前时间")
        r = evaluate("上证指数-1.59%。", freshness={"analyzed_at": "2026-10-15T10:30:00+00:00"})
        check("带时区时间转中国本地口径", r["matched"], 1, "UTC 10:30 等于北京时间18:30")

        for text in ("上证指数下跌-1.59%。", "上证指数下跌1.59%。", "上证指数−1.59%。",
                     "上证指数跌幅为1.59%。", "上证指数下跌了1.59%。", "上证指数下跌约 1.59%。",
                     "上证指数下跌：1.59%。", "上证指数下跌: 1.59%。",
                     "上证指数下跌幅度为1.59%。", "上证指数下跌幅度约1.59%。"):
            check(f"负号正确 {text}", evaluate(text)["matched"], 1, "负号不能再次取反")
        r = evaluate("上证指数上涨-1.59%。")
        check("符号与文字冲突不下结论", (r["unverifiable"], r["numbers_found"]), (1, 0),
              "冲突文本不能强行判对或判错")
        for text in ("上证指数涨跌幅+1.59%。", "上证指数涨跌幅约+1.59%。",
                     "上证指数涨跌幅度为：+1.59%。", "上证指数上涨幅度为1.59%。"):
            row = db.query(MarketData).filter_by(symbol="sh000001", date=datetime(2026, 10, 15)).one()
            row.change_pct = 1.59
            db.commit()
            check(f"中性涨跌幅字段不反转 {text}", evaluate(text)["matched"], 1,
                  "涨跌幅字段名不表示下跌方向")
            row.change_pct = -1.59
            db.commit()
        for text in ("上证指数涨跌幅1.59%。", "上证指数涨跌幅约1.59%。",
                     "上证指数1.59%。", "上证指数未下跌1.59%。", "上证指数并非上涨-1.59%。",
                     "上证指数下跌1.59%，深证成指1.59%。"):
            r = evaluate(text)
            check(f"方向含糊不默认上涨 {text}", r["unverifiable"] >= 1, True,
                  "无符号中性标签、否定或缺方向不得继承其他分句的涨跌词")
            check(f"含糊数字不制造不一致 {text}", r["mismatched"], [],
                  "无法确认应计不可验证，不强行判错")
        # 即使有行情且幅度绝对值相同，也不能把上一分句的跌复制到新标的。
        db.add(MarketData(symbol="sz399001", date=datetime(2026, 10, 15), change_pct=-1.59))
        db.commit()
        r = evaluate("上证指数下跌1.59%，深证成指1.59%。")
        check("不同标的之间不继承方向", (r["matched"], r["numbers_found"], r["unverifiable"]),
              (1, 1, 1), "第二标的有行情但无方向，仍须不可验证")
        db.query(MarketData).filter_by(symbol="sz399001").delete()
        db.commit()
        for text in ("深证成指-1.59%。", "未知指标-1.59%。", "上证指数昨日-1.59%。",
                     "2026-10-14上证指数-1.59%。", "上证指数下跌1.59%（昨日收盘）。",
                     "上证指数下跌1.59%，为昨日表现。", "上证指数下跌1.59%（10月14日）。"):
            r = evaluate(text)
            check(f"缺数据或引用不明确 {text}", (r["status"], r["matched"]), ("skipped", 0),
                  "缺行情、无主语或其他日期不能靠数值碰巧命中算通过")
        r = evaluate("上证指数-0.88%。")
        check("错误数字仍被发现", (len(r["mismatched"]), r.get("flag")),
              (1, "数字与对应行情不一致，需复核"), "消除误报不能把真实不一致过滤掉")
        row = db.query(MarketData).filter_by(symbol="sh000001", date=datetime(2026, 10, 15)).one()
        for missing in (None, float("inf")):
            row.change_pct = missing
            db.commit()
            r = evaluate("上证指数-1.59%。")
            check(f"缺指标/非有限值 {missing} 不计分母", (r["unverifiable"], r["numbers_found"]),
                  (1, 0), "没有可比较值就不能判不一致")
        row.change_pct = -1.59
        db.commit()
        report.report_day = date(2026, 10, 14)
        r = evaluate("上证指数-1.59%。")
        check("旧补跑日期歧义不推算配置时间", r["status"], "skipped", "当前REPORT_TIME不能冒充历史配置")
        report.report_day = date(2026, 10, 15)
        row.date = datetime(2026, 10, 14)
        db.commit()
        r = evaluate("上证指数-1.59%。")
        check("只有旧日线不能验证当日A股", r["numbers_found"], 0, "向后匹配不能放行过期当日数据")

        report.content = json.dumps({"market_summary": "利好未传导，人民币走强；最终防守等待。"})
        db.commit()
        r = ev.check_internal_consistency(db)
        check("词频相反仅要求复核", (r["needs_review"], r["inconsistent"][0]["verdict"], "flag" in r),
              (True, "needs_review", False), "词频不理解否定和转折，不能确定语义矛盾")
        check("保留旧字段但说明指标口径", (r["metric_scope"], r["semantic_verdict"]),
              ("lexical_agreement", "not_determined"), "旧字段名不再代表已确认语义一致率")
        for text in ("", "市场分化，等待确认。", "上涨与下跌并存。"):
            report.content = json.dumps({"market_summary": text})
            db.commit()
            r = ev.check_internal_consistency(db)
            check(f"词频方向不明 {text!r} 不算通过", (r["status"], r["checked"], r["consistency_rate"]),
                  ("skipped", 0, None), "空文本、零命中或多空打平均不计入分母")
        check("方法版本进入结果", r["method_version"], ev.EVALUATION_METHOD_VERSION,
              "新旧评估口径不可直接混用")
    engine.dispose()


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

    # 先判库文件在不在 —— **必须赶在 import app.db 之前**。
    # 原因：`app/db.py` 一被 import 就会 `mkdir` 库文件的父目录，而 SQLite 首次连接
    # 会直接把文件建出来。于是「只是想核对一下结构」会**留下垃圾** ——
    # 库不存在时凭空多出一个空库和一个空目录。2026-09-21 实测确认过。
    # 本脚本不该有副作用，所以库不在就 SKIP，连碰都不碰。
    from app.config import settings
    db_path = _sqlite_path(settings.database_url)
    if db_path is not None and not db_path.exists():
        _SKIPS.append(f"结构哨兵（库文件不存在：{db_path.name}，未创建）")
        print(f"  [SKIP] 库文件不存在（{db_path.name}），不创建、直接跳过")
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
    _s5_a2()
    _a3()
    _b2()
    _s8()
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
