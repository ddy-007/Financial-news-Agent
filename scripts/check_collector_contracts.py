"""采集层判定契约自检：把设计文档 §9/§11 的判定从「文字描述」变成可执行断言。

用法：`.venv/Scripts/python.exe scripts/check_collector_contracts.py`
退出码 0 = 全绿；非 0 = 有契约被改动或破坏。

**完全不联网、不调 LLM、不碰 `data/app.db`** —— 全部用假 client 与内存 SQLite。

**为什么需要它**：设计文档用文字描述判定规则，代码用 if/elif 实现同一件事。
这是同一份事实的两个副本，靠人记着同步。2026-09-22 实施 P0–P2 时，
断言只在会话里跑过、**跑完就删**，结果：
- 下次改采集层无法复跑，判断不了是否退化；
- 独立验收方只能临时重造（造出来了，但也没进仓库）。

**没有留档的断言 ≈ 没有断言。** 这与 `check_eval_contracts.py` 是同一个理由、同一种形态。

**诚实边界**：测的是**判定分支**，不测与真实源的交互（那由 `check_news_sources.py`
与 `verify.py` 负责）。四道 guard 的「触顶不抛错」用假 client 模拟，不代表真实源行为。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parents[1]

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models  # noqa: E402,F401  触发模型注册（CollectorState 要建表）
from app.collectors.news_collector import (  # noqa: E402
    Anchor, ClsCollector, EastmoneyCollector, NewsItem, SinaCollector,
    SourceResult, _cutoff, _validate,
)
from app.config import settings  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.collector_state import CollectorState  # noqa: E402
from app.services.news_service import load_states, save_states  # noqa: E402

_RESULTS: list[tuple[bool, str]] = []


def check(label: str, got, want, contract: str = "") -> None:
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if contract:
        print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


# ============ 假 client ============
class _Resp:
    def __init__(self, d):
        self._d = d

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class FakeClient:
    """按调用次序返回预置页；`pages` 里的元素是 dict（返回该页）或 Exception（该页抛错）。

    超出长度后**一直重复最后一页** —— 所以「抛错页」必须放在最末：
    放在中间的话，`_get_json` 的 3 次重试会打到后面的正常页上、重试成功，
    `failed_pages` 仍是 0（实施时踩过这个坑）。
    """
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    def get(self, url, **kw):
        p = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        if isinstance(p, Exception):
            raise p
        return _Resp(p)


_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_EPOCH = int(datetime.now().timestamp())


def _sina_page(rows):
    return {"result": {"data": {"feed": {"list": rows}}}}


def _sina_row(**kw):
    d = {"rich_text": "【标题】正文", "docurl": "http://s/1", "create_time": _TS}
    d.update(kw)
    return d


def _em_page(rows, cursor):
    return {"data": {"sortEnd": cursor, "fastNewsList": rows}}


def _em_row(**kw):
    d = {"code": "C1", "title": "T", "summary": "S", "showTime": _TS}
    d.update(kw)
    return d


def _cls_page(rows):
    return {"data": {"roll_data": rows}}


def _cls_row(**kw):
    d = {"id": 1, "ctime": _EPOCH, "title": "T", "brief": "B"}
    d.update(kw)
    return d


# ============ A. 四道 guard ============
def _guards():
    print("\nA. 四道 guard")

    # ① 空页 → 正常结束（不是异常、不是跑到上限）
    fake = FakeClient([_sina_page([_sina_row()]), _sina_page([])])
    r = SinaCollector().fetch(client=fake)
    check("① 空页即结束（请求 2 次）", fake.calls, 2, "§9.1-①：空页 → break")
    check("① 结束时不报截断", r.truncated, False)

    # ② 同一批里 id 重复 → 只留一条
    fake = FakeClient([_em_page([_em_row(code="C9"), _em_row(code="C9")], "S1"),
                       _em_page([], "S2")])
    r = EastmoneyCollector().fetch(client=fake)
    check("② 稳定 id 去重", len(r.items), 1, "§9.1-②：seen 按稳定 id 去重")

    # ③ 游标不前进 → 停止翻页（不跑满上限）
    fake = FakeClient([_em_page([_em_row()], "SAME"), _em_page([_em_row(code="C2")], "SAME")])
    r = EastmoneyCollector().fetch(client=fake)
    check("③ 游标未前进即停（请求 2 次）", fake.calls, 2,
          "§9.1-③：next_cursor == cursor → break")
    fake = FakeClient([_cls_page([_cls_row()]), _cls_page([_cls_row(id=2)])])
    r = ClsCollector().fetch(client=fake)
    check("③ 财联社同样（ctime 未前进即停）", fake.calls, 2, "同上")

    # ④ 触顶不抛错 + truncated=True
    old = settings.news_max_pages
    settings.news_max_pages = 3
    try:
        fake = FakeClient([_em_page([_em_row(code=f"C{i}")], f"S{i}") for i in range(1, 6)])
        r = EastmoneyCollector().fetch(client=fake)
        check("④ 触顶不抛错、正常返回", fake.calls, 3,
              "§9.1-④：撞页上限 → 取到多少算多少，不 raise")
        check("④ truncated=True", r.truncated, True, "撞上限必须置 truncated")
    finally:
        settings.news_max_pages = old


# ============ B. 逐页容错 ============
def _page_tolerance():
    print("\nB. 逐页容错（两类分页行为不同）")

    # 页面型（新浪）：继续下一页，已得页保留；连续 3 页失败才停
    fake = FakeClient([_sina_page([_sina_row()]), RuntimeError("down")])
    r = SinaCollector().fetch(client=fake)
    check("页面型：已得页保留", len(r.items), 1, "§9.2：单页失败不丢已得页")
    check("页面型：连续 3 页失败即停", r.failed_pages, 3,
          "连续失败上限 MAX_CONSECUTIVE_PAGE_FAILS=3（防源挂掉时白跑满 20 页）")
    check("页面型：有失败页则 ok=False", r.ok, False,
          "ok 判据是「无任何失败页」—— 中间空洞不可回补")
    check("页面型：请求数 = 1 成功 + 3 页 × 3 重试", fake.calls, 10,
          "⚠️ 一「页失败」实际打 3 次 get（@with_retry(retry_times=3)）")

    # 游标型（东财）：某页失败即 break —— 游标没推进，continue 等于空转
    fake = FakeClient([_em_page([_em_row()], "S1"), RuntimeError("down")])
    r = EastmoneyCollector().fetch(client=fake)
    check("游标型：已得页保留", len(r.items), 1, "同上")
    check("游标型：某页失败即停（failed_pages=1）", r.failed_pages, 1,
          "§9.2 v3.1 修正：游标型跳不过失败页（游标未推进），不能 continue")
    check("游标型：请求数 = 1 + 3 重试（不是拿同一游标打 20 次）", fake.calls, 4,
          "这是 v3.1 修掉的空转缺陷")


# ============ C. 边界校验 ============
def _validation():
    print("\nC. 边界校验（§9.4）")
    now = datetime.now()

    def _mk(title="标题", t=now, url="http://u"):
        return NewsItem(title=title, source="s", url=url, publish_time=t)

    def _reason(item):
        """取拒绝原因；**放行时返回 None**。

        别写成 `_validate(...).reason` —— 那样一旦校验被改坏（不再拒绝），
        取属性会抛 AttributeError **把整条脚本崩掉**，而不是给出一条 FAIL。
        「改动 → 崩溃」和「改动 → 一条清晰的 FAIL」对排查的价值差很多。
        """
        r = _validate("s", item, {})
        return getattr(r, "reason", None)

    check("正常记录 → 放行", _validate("s", _mk(), {}), None, "合格返回 None")
    check("空标题 → empty_title", _reason(_mk(title="   ")), "empty_title",
          "空标题会一路进 LLM 分类 prompt，是纯噪音")
    check("时间为 None → bad_publish_time", _reason(_mk(t=None)), "bad_publish_time",
          "不可解析**不回退为 now()** —— 回退会把来历不明的时间当事实写进库")
    check("超前 30 天 → future_publish_time",
          _reason(_mk(t=now + timedelta(days=30))), "future_publish_time",
          "未来时间会把水位线钉死（每轮只抓 1 页且回不来）")
    check("超前 2 小时 → 放行（容差内）",
          _validate("s", _mk(t=now + timedelta(hours=2)), {}), None,
          "容差 1 天，避免误伤正常时钟偏差")
    it = _mk(url="   ")
    _validate("s", it, {})
    check("空白 url → 归一为 None", it.url, None,
          "下游 _item_to_news 靠 None 避开唯一约束冲突")


# ============ D. cutoff 推导 ============
def _cutoff_contract():
    print("\nD. _cutoff（增量规则唯一落地处）")
    now = datetime.now()
    check("无锚点 → now - lookback_days",
          abs((now - _cutoff(None)) - timedelta(days=settings.news_lookback_days))
          < timedelta(seconds=5), True, "§11：首次/无水位线时回退")
    T = datetime(2026, 9, 22, 12, 0, 0)
    check("有锚点 → last_ts - overlap",
          _cutoff(Anchor(last_ts=T)),
          T - timedelta(minutes=settings.news_overlap_minutes),
          "§11：cutoff = last_ts - news_overlap_minutes")
    check("锚点 last_ts 为 None → 回退",
          abs((_cutoff(Anchor(last_ts=None))
               - (now - timedelta(days=settings.news_lookback_days)))) < timedelta(seconds=5),
          True, "锚点存在但为空，等同于没有")
    check("未来锚点 → 取 min 兜底，cutoff 不落在未来",
          _cutoff(Anchor(last_ts=now + timedelta(days=365))) < now, True,
          "防历史脏数据把 cutoff 推到未来")


# ============ E. 水位线推进规则 ============
def _state_rules():
    print("\nE. save_states 推进规则（§11 规则表）")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)

    def sr(source="新浪财经", n=3, ok=True, failed=0, truncated=False):
        base = datetime(2026, 9, 22, 12, 0, 0)
        items = [NewsItem(title=f"T{i}", source=source,
                          publish_time=base + timedelta(minutes=i)) for i in range(n)]
        return SourceResult(source=source, items=items, rejected=[],
                            ok=ok, failed_pages=failed, truncated=truncated)

    def rows(db):
        return {r.source: r for r in db.query(CollectorState).all()}

    # 规则 4：正常推进
    db = Sess()
    save_states(db, [sr(n=3)], classify_failed=0)
    st = load_states(db)["新浪财经"]
    check("规则 4：last_ts = 本轮最大 publish_time", st.last_ts,
          datetime(2026, 9, 22, 12, 2, 0), "§11 规则 4")
    check("规则 4：empty_streak 归零", rows(db)["新浪财经"].empty_streak, 0, "同上")
    db.close()

    # 成功但 0 条
    db = Sess()
    save_states(db, [sr(n=3)], classify_failed=0)
    before = load_states(db)["新浪财经"].last_ts
    save_states(db, [sr(n=0)], classify_failed=0)
    check("0 条：last_ts 不变", load_states(db)["新浪财经"].last_ts, before,
          "§11：成功但 0 条 → empty_streak+1，last_ts 不变")
    check("0 条：empty_streak+1", rows(db)["新浪财经"].empty_streak, 1, "同上")
    db.close()

    # 规则 1：采集不完整
    db = Sess()
    save_states(db, [sr(n=3)], classify_failed=0)
    b_ts, b_ok = load_states(db)["新浪财经"].last_ts, rows(db)["新浪财经"].last_ok_at
    save_states(db, [sr(n=3, ok=False, failed=2)], classify_failed=0)
    check("规则 1：ok=False → last_ts 不变", load_states(db)["新浪财经"].last_ts, b_ts,
          "§11 规则 1：下轮重取同窗口")
    check("规则 1：last_ok_at 也不更新", rows(db)["新浪财经"].last_ok_at, b_ok, "同上")
    db.close()

    # 规则 2：分类失败 → 全部源不推进（且抢先于规则 3）
    db = Sess()
    save_states(db, [sr("新浪财经", 3), sr("东方财富", 3)], classify_failed=0)
    before_ts = {k: v.last_ts for k, v in load_states(db).items()}
    save_states(db, [sr("新浪财经", 5, truncated=True), sr("东方财富", 5)],
                classify_failed=7)
    check("规则 2：全部源 last_ts 均不变",
          {k: v.last_ts for k, v in load_states(db).items()}, before_ts,
          "§11 规则 2：条目未入库，推进即永久丢失")
    db.close()

    # 规则 3：撞页上限 → 推进 + truncated_at
    db = Sess()
    save_states(db, [sr(n=3, truncated=True)], classify_failed=0)
    check("规则 3：截断仍推进 last_ts", load_states(db)["新浪财经"].last_ts,
          datetime(2026, 9, 22, 12, 2, 0),
          "§11 规则 3：显式例外，否则与「不推进」合起来会死锁")
    check("规则 3：truncated_at 被写入", rows(db)["新浪财经"].truncated_at is not None,
          True, "供 P3 监控")
    db.close()

    # 恢复正常后 truncated_at 要清空
    db = Sess()
    save_states(db, [sr(n=3, truncated=True)], classify_failed=0)
    save_states(db, [sr(n=4, truncated=False)], classify_failed=0)
    check("恢复正常 → truncated_at 归 None",
          rows(db)["新浪财经"].truncated_at, None, "不残留旧告警")
    db.close()


# ============ F. 分类失败的兜底链路 ============
def _classify_fallback():
    """锁定「解析失败也能切备用」这条不变量。

    **它今天刚被破坏过**：`llm_retry_times()` 把重试降为 1 的前提是
    「失败时备用会顶上一次机会」。而 JSON 解析失败（`ValueError`）原先不在
    `_FALLBACK_EXCEPTIONS` 里 → 备用不触发 + 重试又被降成 1 → **合计只有 1 次机会**，
    比没配备用时的 3 次还少。**启用备用反而降低了这类失败的容错。**

    2026-09-22 实机：一轮 110 批里有一批因此丢掉 20 条（好在 §11 规则 2 让水位线
    不推进，下轮重取了回来）。
    """
    print("\nF. 分类失败的兜底链路")
    from app.agent.llm import _FALLBACK_EXCEPTIONS
    check("解析失败（ValueError）会触发切备用",
          issubclass(ValueError, _FALLBACK_EXCEPTIONS), True,
          "否则「重试降为 1」的前提不成立 —— 解析失败会零兜底直接放弃整批")

    # 行为验证（不是查源码里有没有某个词）：让 LLM 抛解析异常，看条目去哪了
    import app.agent.data_agent as DA
    batch = [NewsItem(title=f"T{i}", source="新浪财经",
                      publish_time=datetime(2026, 9, 22, 12, i)) for i in range(3)]
    orig_llm, orig_retry = DA.get_llm, DA.call_with_retry
    try:
        DA.get_llm = lambda **kw: None
        DA.call_with_retry = lambda *a, **kw: (_ for _ in ()).throw(
            ValueError("LLM 分类输出非数组"))
        ok_res, failed = DA.classify_news(batch)
    finally:
        DA.get_llm, DA.call_with_retry = orig_llm, orig_retry
    check("解析失败 → 3 条全部进 failed（不静默消失）",
          (len(ok_res), len(failed)), (0, 3),
          "§17：条目必须交回上层，才谈得上「水位线不推进、下轮重取」")

    # 熔断：LLM 连续失败即中断本轮，**但剩余条目必须一并交出来**
    # 2026-09-22 实测：连接中断后 0.43 秒刷出 110 条报错、55 批全废。
    from app.agent.data_agent import BATCH_SIZE, MAX_CONSECUTIVE_BATCH_FAILS
    n_batches = MAX_CONSECUTIVE_BATCH_FAILS + 2          # 故意多两批，验证"剩下的被中断"
    big = [NewsItem(title=f"T{i}", source="新浪财经",
                    publish_time=datetime(2026, 9, 22, 12, 0, 0)) for i in range(BATCH_SIZE * n_batches)]
    orig_retry = DA.call_with_retry
    calls: list[int] = []
    try:
        DA.get_llm = lambda **kw: None

        def _fail(*a, **kw):
            calls.append(1)
            raise RuntimeError("Connection error.")
        DA.call_with_retry = _fail
        ok_res, failed = DA.classify_news(big)
    finally:
        DA.get_llm, DA.call_with_retry = orig_llm, orig_retry

    # ⚠️ 这两条必须**分开**：只看 `failed` 的条数是测不出熔断的
    # （跑满全部批次 vs 提前中断，最终 failed 都是全量）。
    # 「有没有提前中断」只能靠**调用次数**看出来 —— 这是变异测试逼出来的。
    check("连续失败即熔断：只打了 N 批就停下，没跑满",
          len(calls), MAX_CONSECUTIVE_BATCH_FAILS,
          f"共 {n_batches} 批，应在第 {MAX_CONSECUTIVE_BATCH_FAILS} 批后中断"
          f"（2026-09-22 实测：不熔断时 0.43 秒刷出 110 条报错）")
    check("熔断时**剩余条目也进 failed**（一条都不能漏）",
          len(failed), len(big),
          f"剩下 {n_batches - MAX_CONSECUTIVE_BATCH_FAILS} 批必须一并交出 ——"
          f"否则 classify_failed 偏小、水位线照常推进、那批新闻永久丢失")
    check("熔断时没有任何条目被当成分类成功", len(ok_res), 0,
          "失败就是失败，不能混进 results")


# ============ G. 采集互斥 ============
def _mutex():
    """锁定「同一时刻只允许一次采集」。

    2026-09-22 实机撞上过两轮重叠：手动端点 `POST /news/collect` 绕过 APScheduler 的
    `max_instances=1`，手动触发的那轮还没跑完，定时的那轮又起来了 ——
    LLM 调用翻倍、并发写同一个 SQLite、竞争同一份水位线。
    """
    print("\nG. 采集互斥（定时任务 vs 手动端点）")
    import threading

    import app.agent.data_agent as DA
    import app.services.news_service as NS

    # ⚠️ 必须给**真的内存库**，不能像以前那样传 `db=None`：
    # P3（2026-09-22）起 `collect_and_store_news` 末尾会做源健康巡检，
    # 那条路径真的读 `collector_state` —— 传 None 会 AttributeError。
    # 空表时 `evaluate` 判为健康、不打日志，对本用例无副作用。
    #
    # ⚠️ 必须用 StaticPool：`sqlite:///:memory:` 默认是 **每线程一个独立库**，
    # 子线程里建的表另一个线程看不见（实测报 `no such table: collector_state`）。
    # StaticPool 让所有线程共用一个连接，内存库才真的共享。
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)

    orig = (NS.run_collection, NS.save_states, DA.run_data_agent)
    started, release = threading.Event(), threading.Event()
    try:
        def slow_collect(db):
            started.set()
            release.wait(timeout=5)      # 卡住，模拟"一轮要跑几十分钟"
            return [], []
        NS.run_collection = slow_collect
        NS.save_states = lambda *a, **k: None
        DA.run_data_agent = lambda db, raw=None: {"new": 7, "classify_failed": 0}

        out = {}
        t = threading.Thread(
            target=lambda: out.__setitem__("A", NS.collect_and_store_news(Sess())))
        t.start()
        started.wait(timeout=5)          # 等 A 真的进到采集里
        out["B"] = NS.collect_and_store_news(Sess())   # 并发再调一次 → 应被跳过
        release.set()
        t.join(timeout=5)
    finally:
        NS.run_collection, NS.save_states, DA.run_data_agent = orig

    check("并发时先到的正常跑完", out.get("A"), 7, "第一个拿锁的照常执行")
    check("后到的被跳过（返回 0，不排队）", out.get("B"), 0,
          "非阻塞获取：一轮几十分钟，等它对调用方毫无意义")


# ============ H. 连接类故障自愈 + 异常描述 ============
def _connection_selfheal():
    """锁定「连接类故障 → 丢弃共享 client」这条自愈路径（2026-09-26 实机）。

    **背景**：后端连续运行几小时后，三个源**一起**变成 TLS 握手超时，
    重启后端立刻恢复；同一台机器上新起的进程一切正常。坏的是**进程内的 client
    对象**（连接池 / SSL 上下文 / 代理挂载在创建时就固定了），不是外网。

    这里钉两件事：
    ① 异常描述必须带**类型名与 cause 链** —— 否则下次还是查不出根因
       （实测日志只剩 `[重试] 新闻接口 重试 3 次后仍失败:`，冒号后是空的）；
    ② 连接类故障必须重置单例，且**只对内部单例**重置 ——
       注入的 client 是调用方自己的对象。
    """
    print("\nH. 连接类故障自愈 + 异常描述（2026-09-26）")
    import httpx

    from app.collectors import http_client as HC
    from app.collectors import news_collector as NC
    from app.retry import describe_exception

    # ---- ① 异常描述 ----
    try:
        try:
            raise ValueError("底层原因")
        except ValueError as inner:
            raise RuntimeError("外层") from inner
    except RuntimeError as e:
        desc = describe_exception(e)
    check("describe_exception 以类型名开头", desc.startswith("RuntimeError: "), True,
          "只打 {e} 时类型信息全丢")
    check("describe_exception 追到 cause",
          ("ValueError" in desc and "底层原因" in desc), True,
          "httpx → httpcore → ssl/socket 的链条必须可见，否则分不出 DNS/TLS/读超时")
    check("空消息异常也留下类型名",
          describe_exception(httpx.ConnectTimeout("")).startswith("ConnectTimeout"),
          True,
          "httpx 重包 httpcore 时 str(e) 常为空串 —— 本次故障正是这一类")

    # ---- ② 连接类判定（按类型，不看文本）----
    _req = httpx.Request("GET", "https://example.com")
    _404 = httpx.HTTPStatusError(
        "404", request=_req, response=httpx.Response(404, request=_req))
    check("ConnectError → 连接类", HC.is_connection_error(httpx.ConnectError("x")),
          True)
    check("ConnectTimeout → 连接类",
          HC.is_connection_error(httpx.ConnectTimeout("")), True,
          "本次故障正是 TLS 握手超时")
    check("ReadTimeout → 连接类", HC.is_connection_error(httpx.ReadTimeout("")), True)
    check("HTTP 404 → **不是**连接类", HC.is_connection_error(_404), False,
          "服务端已明确回应，换个连接打过去还是 404 —— 重建纯属浪费")
    check("普通异常 → **不是**连接类", HC.is_connection_error(RuntimeError("x")),
          False)

    # ---- ③ 重置确实换了一个新对象 ----
    before = HC.get_client()
    HC.reset_client()
    after = HC.get_client()
    check("reset_client 后拿到的是新对象", before is not after, True,
          "旧连接池 / SSL 上下文 / 代理挂载一并丢弃 —— 这就是「重启才恢复」的自动化")
    check("旧对象已被关闭", before.is_closed, True, "不能只丢引用不关连接")

    # ---- ④ 接线：只有内部单例才重置 ----
    # ⚠️ 绕开 @with_retry 的 1s/2s 退避，直接测函数体 —— 否则本用例要白等 3 秒。
    raw = NC.SinaCollector._get_json.__wrapped__
    col = NC.SinaCollector()
    resets: list[str] = []
    orig_get, orig_reset = NC.get_client, NC.reset_client
    try:
        NC.reset_client = lambda: resets.append("reset")

        NC.get_client = lambda: FakeClient([httpx.ConnectError("boom")])
        try:
            raw(col, "https://x", client=None)
        except httpx.ConnectError:
            pass
        check("内部单例 + 连接类故障 → 触发 reset", resets, ["reset"],
              "不重置就退化成「必须人工重启后端」")

        resets.clear()
        try:
            raw(col, "https://x", client=FakeClient([httpx.ConnectError("boom")]))
        except httpx.ConnectError:
            pass
        check("注入的 client 故障 → **不** reset", resets, [],
              "测试/脚本注入的对象归调用方管，关掉它会污染调用方后续使用")

        resets.clear()
        NC.get_client = lambda: FakeClient([RuntimeError("server said 500")])
        try:
            raw(col, "https://x", client=None)
        except RuntimeError:
            pass
        check("非连接类故障 → **不** reset", resets, [],
              "只有连接层面坏了才值得换一条连接")

        # ---- ⑤ 端到端：坏 client → 重置 → **第二次尝试就成功** ----
        # 这条是本改动的**全部目的**：把「三源全挂、必须人工重启后端」变成自愈。
        # 走**装饰过的**函数（不绕退避），因为要验的正是重试之间的重建时序。
        # ⚠️ 若把重建挪到一轮结束时，这里会看到第二次尝试仍用坏 client → FAIL。
        bad = FakeClient([httpx.ConnectError("boom")])
        good = FakeClient([_sina_page([_sina_row()]), _sina_page([])])
        handed: list = []

        def _next_client():
            c = [bad, good][min(len(handed), 1)]
            handed.append(c)
            return c

        resets.clear()
        NC.get_client = _next_client
        got = NC.SinaCollector._get_json(col, "https://x")
        check("第一次失败 → 重置 → 第二次用新 client 并成功",
              (resets, len(handed), [c is bad for c in handed], isinstance(got, dict)),
              (["reset"], 2, [True, False], True),
              "实测故障就是这个形态：坏 client 让三个源一起 TLS 超时，重启才好")
    finally:
        NC.get_client, NC.reset_client = orig_get, orig_reset
        HC.reset_client()          # 收尾：别把本用例造出来的 client 留成单例

    # ---- ⑤ 源码守卫：日志不能退回只打 {e} ----
    nc_src = (ROOT / "app" / "collectors" / "news_collector.py").read_text(
        encoding="utf-8")
    retry_src = (ROOT / "app" / "retry.py").read_text(encoding="utf-8")
    # ⚠️ 这里断言的是**不变量**（不许再格式化裸异常），不是次数。
    # 第一版写死「等于 3」—— 结果 `_get_json` 里的重置告警也算一处、共 4 处，
    # 报了一条假 FAIL。次数会随调用点增减漂移，**不变量不会**。
    check("采集层不再直接格式化裸异常", "{e}" in nc_src, False,
          "逐页失败（新浪/东财/财联社）+ 帧级失败 + 重置告警共 5 处，"
          "漏一处那一路故障就又变成无字天书")
    check("重试的最终失败带异常描述",
          "describe_exception(last)" in retry_src, True,
          "「重试 3 次后仍失败:」后面原来是空的")


def main() -> int:
    _guards()
    _page_tolerance()
    _validation()
    _cutoff_contract()
    _state_rules()
    _classify_fallback()
    _mutex()
    _connection_selfheal()
    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
