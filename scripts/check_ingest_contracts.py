"""入库索引层判定契约自检：把《新闻采集中入库索引重构设计》的判定变成可执行断言。

用法：`.venv/Scripts/python.exe scripts/check_ingest_contracts.py`
退出码 0 = 全绿；非 0 = 有契约被改动或破坏。

**完全不联网、不调 LLM、不碰 `data/app.db`** —— 内存 SQLite + 假向量库 + 假 LLM。

**为什么需要它**（与 `check_collector_contracts.py` 同一个理由）：
设计文档用文字描述规则，代码用 if/elif 实现同一件事 —— 同一份事实的两个副本，
靠人记着同步。2026-09-22 前面几轮反复出现「改了一处、另一处没跟上」，
所以把判定钉成断言。

**诚实边界**：测的是**判定分支与顺序**，不测真实嵌入质量、不测 Chroma 真实行为、
不测与真实 LLM 的交互。相似度阈值取 0.85/0.70 是经验值，本脚本只验证
「≥0.85 直接合并 / 灰区交 LLM / <0.70 算新」这三条分支走得对，
**不代表真实语料下阈值选得对**。

⚠️ **已知并已接受的行为**（2026-09-22 用户拍板，**不要再当缺陷报**）：

    标题相同但正文不同的条目**会被合并** —— 典型是**每日固定栏目**
    （`今日特朗普要闻` / `今日投资舆情热点` / `周一重要财经信息提醒`）
    与《轻工纺织…》解读的不同问答。实测库里共 **55 对 / 29 个标题（15%）**。

    真机实测（bge-m3）：在「标题+正文」嵌入下，这类对与**真正的跨源同事件**
    **余弦分布重叠** —— 不该合的最高 **0.9845**、应合的最低 **0.9819**，
    所以**单纯调阈值分不开**。改用「仅正文（剥【标题】前缀）」嵌入可以分开
    （0.9456 vs 0.9681），但用户判断「内容相差不会很多」，**决定不修**。

    后果（如实用例，仅记录不再重复）：每日栏目每个标题只留一行，
    `source_count` 随天数累积，专家 prompt 里仍显示「（N源）」。
"""
import hashlib
import json
import random
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import sqlalchemy
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models  # noqa: E402,F401  触发模型注册
import app.agent.data_agent as DA  # noqa: E402
from app.collectors.news_collector import NewsItem  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.news import News  # noqa: E402

_RESULTS: list[tuple[bool, str]] = []


def check(label: str, got, want, contract: str = "") -> None:
    ok = got == want
    _RESULTS.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if contract:
        print(f"         契约：{contract}")
    if not ok:
        print(f"         got={got!r}  want={want!r}")


# ============ 假嵌入 / 假向量库 ============
_VEC: dict[str, list[float]] = {}
_DIM = 16


def unit(*vals) -> list[float]:
    """补零到 _DIM 并归一化。"""
    v = list(vals) + [0.0] * (_DIM - len(vals))
    a = np.asarray(v, dtype=np.float32)
    return (a / np.linalg.norm(a)).tolist()


def register(title: str, content: str, vec: list[float]) -> None:
    """把某条 (title, content) 的**入库文本**绑到一个确定向量上。

    ⚠️ 必须用 `_doc_text` 拼 —— `_VEC` 的键是 `Candidate.text`，
    手动拼字符串很容易少个换行，然后候选拿到随机向量、静默匹配不上
    （本脚本第一版就是这么错的：4 个用例里有 2 个是这么假失败的）。
    """
    _VEC[DA._doc_text(title, content)] = vec


def reset_vectors() -> None:
    """清空向量登记表。

    **每个用例组开头都要调** —— 否则上一组登记的向量会漏到下一组，
    制造出「看起来像代码 bug、其实是用例串味」的假失败。
    """
    _VEC.clear()


def fake_embed(texts: list[str]) -> list[list[float]]:
    """注册过的文本用注册向量；没注册的按文本哈希生成**互相近似正交**的随机向量。

    随机向量在高维下两两余弦 ≈ 0，稳稳落在 GRAY_LOW 以下 —— 也就是"互不相似"，
    这正是未注册条目该有的行为。
    """
    out = []
    for t in texts:
        v = _VEC.get(t)
        if v is None:
            rnd = random.Random(int(hashlib.sha1(t.encode("utf-8")).hexdigest()[:8], 16))
            v = unit(*[rnd.uniform(-1, 1) for _ in range(_DIM)])
        out.append(list(v))
    return out


class FakeCollection:
    """只实现 `_nearest_existing` 用到的那两个方法。

    Chroma 配的是 `hnsw:space="cosine"`，返回的 distance = 1 − 余弦相似度，
    这里必须复刻这个换算，否则测的就不是真代码的输入。
    """

    def __init__(self, rows: list[tuple[str, list[float]]] | None = None):
        self.rows = list(rows or [])

    def count(self) -> int:
        return len(self.rows)

    def query(self, query_embeddings, n_results):
        q = np.asarray(query_embeddings, dtype=np.float32)
        q = q / np.linalg.norm(q, axis=1, keepdims=True)
        if not self.rows:
            return {"ids": [[] for _ in range(len(q))],
                    "distances": [[] for _ in range(len(q))]}
        m = np.asarray([v for _, v in self.rows], dtype=np.float32)
        m = m / np.linalg.norm(m, axis=1, keepdims=True)
        sims = q @ m.T
        k = min(n_results, len(self.rows))
        ids, dists = [], []
        for row in sims:
            order = np.argsort(-row)[:k]
            ids.append([self.rows[int(i)][0] for i in order])
            dists.append([1.0 - float(row[int(i)]) for i in order])
        return {"ids": ids, "distances": dists}


class FakeStore:
    def __init__(self, coll: FakeCollection):
        self._coll = coll

    def get_collection(self):
        return self._coll


class FakeUpsertStore:
    """只实现 `upsert_documents`，并在收到重复 id 时**像真实 Chroma 一样报错**。

    真实行为见 `chromadb/api/types.py` 的 `validate_ids`：重复 id 抛
    `DuplicateIDError`。这里复刻它，否则「去重」这件事测不出来。
    """

    def __init__(self):
        self.calls: list[list[str]] = []

    def upsert_documents(self, ids, documents, embeddings, metadatas):
        if len(ids) != len(set(ids)):
            raise AssertionError("upsert 收到重复 id（真实 Chroma 会抛 DuplicateIDError）")
        self.calls.append(list(ids))


# ============ 测试替身 ============
@contextmanager
def patched(**kw):
    old = {k: getattr(DA, k) for k in kw}
    for k, v in kw.items():
        setattr(DA, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(DA, k, v)


def make_classifier(relevant: bool = True):
    """假分类器：记录收到的标题，返回与 `classify_news` 同形的结果。"""
    calls: list[list[str]] = []

    def _c(items):
        calls.append([it.title for it in items])
        out = [{"item": it, "relevant": relevant, "category": "综合",
                "market": "无", "themes": []} for it in items]
        return out, []

    return _c, calls


def fresh_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def item(title, content="", source="新浪财经", url=None, pt=None) -> NewsItem:
    return NewsItem(title=title, content=content, source=source, url=url,
                    publish_time=pt or datetime.now())


def add_news(db, title, content, source="新浪财经", pt=None, url=None) -> News:
    n = News(title=title, content=content, source=source, url=url,
             publish_time=pt or datetime.now(), source_count=1,
             source_urls="[]")
    db.add(n)
    db.commit()
    return n


# ============ ① 确定性去重 ============
def _fingerprint():
    print("\n[1] 确定性指纹")

    # 《轻工纺织》反例：**标题相同、正文不同 → 必须不同指纹**
    t = '《轻工纺织产业发展“十五五”规划》解读'
    a = DA._fingerprint(t, "《规划》的总体考虑是什么？我国轻工纺织产业规模体量…")
    b = DA._fingerprint(t, "《规划》如何激发产业融合化发展动力？深化产业融合创新是…")
    check("标题相同但正文不同 → 指纹不同", a == b, False,
          "只按标题去重会误杀真实信息（实测那 6 行里有 4 行是不同问答）")

    # 归一化：空白差异不该产生新指纹
    c = DA._fingerprint("  标题  ", "正文\n\n内容")
    d = DA._fingerprint("标题", "正文 内容")
    check("空白差异被归一化 → 指纹相同", c == d, True)

    # 记录边界：200 字之后的差异**不**影响指纹（设计取舍，不是 bug）
    long_a = "甲" * 200 + "XXXXXXXXXX"
    long_b = "甲" * 200 + "YYYYYYYYYY"
    check("正文前 200 字相同 → 指纹相同（200 字外不参与）",
          DA._fingerprint("T", long_a) == DA._fingerprint("T", long_b), True)


def _group_by_fingerprint():
    print("\n[2] 候选分组")

    # 6 条不同问答 → 6 个候选（**不误杀**）
    t = '《轻工纺织产业发展“十五五”规划》解读'
    items = [item(t, f"《规划》{q}方面有哪些举措？正文{i}",
                  url=None, source="新浪财经")
             for i, q in enumerate("总体考虑 提升创新 融合化 智能化 供需适配 总体考虑".split())]
    # 第 0 与第 5 条正文不同（正文{i} 不同），所以仍是 6 个
    check("6 条标题相同、正文各异的条目 → 6 个候选",
          len(DA._group_by_fingerprint(items)), 6,
          "只按标题去重会把这 6 条并成 1 条，误杀 5 条")

    # 5 条完全同内容、无 url → 1 个候选，且 sources 折叠成一个 (源, None)
    same = [item("同一标题", "同一正文", url=None) for _ in range(5)]
    cands = DA._group_by_fingerprint(same)
    check("5 条同内容无 url → 1 个候选", len(cands), 1,
          "修 A3 成因①：url 为空的条目原先完全没有去重保护")
    check("候选的 sources 已按 (source, url) 折叠",
          len(DA._dedup_sources(cands[0].sources)), 1)

    # 代表条目取正文最长的。注意**只有前 200 字相同才会进同一组** ——
    # 拿三条正文完全不同的条目来测是测不到的：它们压根不是同一个候选。
    a = item("T", "甲" * 200)
    b = item("T", "甲" * 200 + "乙" * 100)
    check("同组候选的代表取正文最长的那个（指纹只看前 200 字）",
          DA._group_by_fingerprint([a, b])[0].item.content, "甲" * 200 + "乙" * 100)
    check("代表不是无脑取第一个", DA._group_by_fingerprint([a, b])[0].item is not a, True)


# ============ ② 语义聚类 ============
def _cluster():
    print("\n[3] 本轮内语义聚类")

    v = lambda *a: np.asarray(unit(*a), dtype=np.float32)  # noqa: E731
    # 0/1 相近（cos=0.95）；2 独立
    vecs = np.stack([v(1, 0), v(0.95, 0.31), v(0, 1)])
    cl = sorted(sorted(c) for c in DA._cluster_by_similarity(vecs))
    check("余弦 0.95 的两条归为一簇，独立的自成簇", cl, [[0, 1], [2]],
          "修「同轮内跨源同事件互相看不见」（实测海光信息三源各一行未合并）")

    # 0.8 落在灰区，不该被聚成"重复"
    vecs2 = np.stack([v(1, 0), v(0.8, 0.6)])
    cl2 = sorted(sorted(c) for c in DA._cluster_by_similarity(vecs2))
    check("余弦 0.80（灰区）不并入，保持两簇", cl2, [[0], [1]])

    check("空输入安全", DA._cluster_by_similarity(np.zeros((0, _DIM), dtype=np.float32)), [])

    # ---- L1（2026-09-24）：传递闭包的规模保护与分布观测 ----
    # 造 k 条**完全相同**的向量 —— 它们两两 cos=1.0，union-find 必然并成一个簇，
    # 正是渗流相变的最小复现（模板化快讯连发时就是这个形态）。
    cap = DA.MAX_CLUSTER_SIZE
    sink = _Sink()
    hid = logger.add(sink.write, level="INFO", format="{message}")
    try:
        over = DA._cluster_by_similarity(np.stack([v(1, 0)] * (cap + 1)))
        at = DA._cluster_by_similarity(np.stack([v(1, 0)] * cap))
    finally:
        logger.remove(hid)
    check(f"{cap + 1} 条全同 → 超过上限，**拒绝自动合并**、降级为逐条",
          sorted(len(c) for c in over), [1] * (cap + 1),
          "误合并是**不可逆**的（条目被吃进代表里），多花几次分类的钱是可接受的代价")
    check(f"正好 {cap} 条 → 不拆（判据是「超过」而非「达到」）",
          [len(c) for c in at], [cap])
    check("簇大小分布已记录（组数 / 最大 / p95 / 中位 四个数）",
          all(k in sink.text for k in ("组数", "最大", "p95", "中位")), True,
          "「最大簇从 14 跳到 9000 就是警报，而这只是一个数字」—— "
          "库内数据**原理上查不出已发生的误合并**（被吃掉的条目已不在库中），"
          "所以这条长期监控是唯一的早期信号")
    check("分布统计的是**保护前**的原始分布（最大簇照实报）",
          f"最大 {cap + 1}" in sink.text, True,
          "拆过之后再统计就永远看不到那个异常大簇了")


# ============ ② 时间窗 ============
def _time_window():
    print("\n[4] 语义匹配的时间窗（修过度合并）")

    reset_vectors()
    now = datetime.now()
    vec = unit(1, 0)
    register("南向资金净买入额达30亿港元", "截至目前，南向资金净买入额达30亿港元。", vec)

    for label, delta, want_match in [
        ("窗口内（1 小时前）", timedelta(hours=1), True),
        ("窗口外（3 天前）", timedelta(days=3), False),
    ]:
        db = fresh_db()
        # 库内那条：向量与候选完全相同（cos = 1.0）
        old = add_news(db, "南向资金净买入额达30亿港元",
                       "截至目前，南向资金净买入额达30亿港元。", pt=now - delta)
        coll = FakeCollection([(old.id, vec)])
        cand = DA.Candidate(item=item("南向资金净买入额达30亿港元",
                                      "截至目前，南向资金净买入额达30亿港元。",
                                      pt=now))
        with patched(vector_store=FakeStore(coll)):
            got = DA._nearest_existing(db, [cand], np.asarray([vec], dtype=np.float32))
        hit = got[0] is not None
        check(f"{label} → {'匹配' if want_match else '不匹配'}", hit, want_match,
              "系统只有 3 个源，source_count>3 结构上不可能是单一事件 —— "
              "没有时间窗时跨天快照被合并成一行，最高到 33")

    # 最近邻在窗口外时，应继续看下一个近邻（不是直接放弃）
    db = fresh_db()
    far = add_news(db, "同标题", "同正文", pt=now - timedelta(days=5))
    near = add_news(db, "同标题", "同正文", pt=now - timedelta(hours=2))
    near_q = (np.asarray(unit(1, 0)) * 0.9 + np.asarray(unit(0, 1)) * 0.1)
    near_q = (near_q / np.linalg.norm(near_q)).tolist()
    coll = FakeCollection([(far.id, vec), (near.id, near_q)])
    with patched(vector_store=FakeStore(coll)):
        got = DA._nearest_existing(db, [DA.Candidate(item=item("同标题", "同正文", pt=now))],
                                   np.asarray([vec], dtype=np.float32))
    check("最近邻在窗口外时继续看下一个近邻（而不是直接放弃）",
          got[0][0].id if got[0] else None, near.id,
          "只取 top-1 会在这种情况下白白丢掉一次合并机会")


# ============ ③ 合并 / 插入 ============
def _merge_and_insert():
    print("\n[5] 合并与插入")

    db = fresh_db()
    existing = add_news(db, "标题", "短正文", source="新浪财经",
                        url="https://a/1")
    longer = DA.Candidate(item=item("标题", "长" * 80, source="东方财富",
                                    url="https://b/2"), sources=[("东方财富", "https://b/2")])
    check("正文更长 → 返回 True（要在末尾重索引）",
          DA._merge_into(db, existing, longer), True,
          "P2：返回值决定该条是否重新 upsert 到 Chroma，修 split-brain")
    check("合并后正文已更新", existing.content, "长" * 80)
    check("合并后源数 2", existing.source_count, 2)

    shorter = DA.Candidate(item=item("标题", "很短的正文", source="财联社",
                                     url="https://c/3"), sources=[("财联社", "https://c/3")])
    check("正文更短 → 返回 False（不必重索引）",
          DA._merge_into(db, existing, shorter), False)
    check("正文未被覆盖", existing.content, "长" * 80)

    # 无 url 的来源不该抬高 source_count
    before = existing.source_count
    no_url = DA.Candidate(item=item("标题", "更长的正文" + "啊" * 90, source="财联社",
                                    url=None), sources=[("财联社", None)])
    DA._merge_into(db, existing, no_url)
    check("无 url 的来源不抬高 source_count", existing.source_count, before,
          "与改动前 `if item.url` 的口径一致，否则同源重复会虚增源数")

    # 多源候选插入
    db2 = fresh_db()
    cand = DA.Candidate(item=item("新标题", "正文", source="新浪财经",
                                  url="https://a/9"),
                        sources=[("新浪财经", "https://a/9"),
                                 ("东方财富", "https://b/9"),
                                 ("财联社", "https://c/9")])
    n = DA._insert_new(db2, cand, {"category": "综合", "market": "无", "themes": []})
    db2.commit()
    check("3 个源各 1 个 url → source_count = 3", n.source_count, 3)

    # ---- source_count 数**去重源数**，不是 (源, url) 对数（2026-09-22 用户拍板） ----
    db3 = fresh_db()
    same_src = DA.Candidate(
        item=item("同源双链", "正文", source="新浪财经", url="https://a/1"),
        sources=[("新浪财经", "https://a/1"), ("新浪财经", "https://a/2")])
    n3 = DA._insert_new(db3, same_src, {"category": "综合", "market": "无", "themes": []})
    db3.commit()
    check("同一源的 2 个 url → source_count = 1（不是 2）", n3.source_count, 1,
          "实测库里有 332 行标着「2源」其实只有 1 个源 —— 会虚高触发 info_level 信号②，"
          "并在专家 prompt 里显示成失真的「（2源）」")

    db4 = fresh_db()
    mixed = DA.Candidate(
        item=item("混合", "正文", source="新浪财经", url="https://a/3"),
        sources=[("新浪财经", "https://a/3"), ("新浪财经", "https://a/4"),
                 ("东方财富", "https://b/3")])
    n4 = DA._insert_new(db4, mixed, {"category": "综合", "market": "无", "themes": []})
    db4.commit()
    check("2 个源、3 个 url → source_count = 2", n4.source_count, 2)

    db5 = fresh_db()
    row = add_news(db5, "T", "正文", source="新浪财经", url="https://a/1")
    DA._merge_into(db5, row, DA.Candidate(
        item=item("T", "更长的正文" + "啊" * 50, source="新浪财经", url="https://a/9"),
        sources=[("新浪财经", "https://a/9")]))
    check("合并「同源的另一个 url」→ source_count 仍是 1", row.source_count, 1,
          "_merge_into 与 _insert_new 必须用同一个口径，否则同一个概念两个算法")


# ============ ④ 主流程顺序 ============
def _pipeline_order():
    print("\n[6] 主流程：顺序调整的收益")

    reset_vectors()
    now = datetime.now()
    vec = unit(1, 0)
    register("同一事件", "同一正文", vec)

    db = fresh_db()
    old = add_news(db, "同一事件", "同一正文", pt=now - timedelta(hours=1))
    coll = FakeCollection([(old.id, vec)])
    classify, calls = make_classifier()

    with patched(embed_documents=fake_embed, vector_store=FakeStore(coll),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: None, _upsert_index=lambda *a, **k: None):
        res = DA.run_data_agent(db, raw=[item("同一事件", "同一正文", pt=now)])

    check("**并入库内已有新闻的候选不进 LLM 分类**",
          calls, [], "这是顺序调整的全部收益来源 —— 实测稳态轮约 68% 的条目是重复")
    check("统计里 merged = 1", res["merged"], 1)

    # 混合场景：1 条重复 + 1 条全新 → 只有新条目进分类
    db = fresh_db()
    old = add_news(db, "同一事件", "同一正文", pt=now - timedelta(hours=1))
    coll = FakeCollection([(old.id, vec)])
    classify, calls = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(coll),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: None, _upsert_index=lambda *a, **k: None):
        res = DA.run_data_agent(
            db, raw=[item("同一事件", "同一正文", pt=now), item("全新事件", "全新正文", pt=now)])
    check("混合轮：只有全新条目进了分类", calls, [["全新事件"]])
    check("混合轮：新增 1、合并 1", (res["new"], res["merged"]), (1, 1))

    # 确定性去重也省分类：同内容三条 → 只分类一次
    db = fresh_db()
    classify, calls = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(FakeCollection()),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: None, _upsert_index=lambda *a, **k: None):
        res = DA.run_data_agent(db, raw=[item("无url事件", "无url正文", url=None)
                                         for _ in range(3)])
    check("同内容无 url 三条 → 只送 1 条进分类", calls, [["无url事件"]])
    check("同内容无 url 三条 → exact_deduped = 2", res["exact_deduped"], 2)


# ============ ⑤ P2 / P3 / fail-open ============
def _p2_p3_failopen():
    print("\n[7] P2 重索引 / P3 按需 BM25 / 灰区 fail-open")

    reset_vectors()
    now = datetime.now()
    vec = unit(1, 0)
    register("标题", "长" * 80, vec)   # P2 用例的候选（正文更长，会改写库内那条）
    register("标题", "短", vec)        # P3 用例 1 的候选（正文更短，不改写）

    # P2：库里的正文更短 → 合并会改写它 → 必须出现在 upsert 列表里
    db = fresh_db()
    old = add_news(db, "标题", "短", pt=now - timedelta(hours=1))
    coll = FakeCollection([(old.id, vec)])
    indexed: list[list[str]] = []
    classify, _ = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(coll),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: None,
                 _upsert_index=lambda rows: indexed.append([r.id for r in rows])):
        DA.run_data_agent(db, raw=[item("标题", "长" * 80, pt=now)])
    check("P2：被改写正文的旧条目出现在 Chroma upsert 列表里",
          indexed, [[old.id]],
          "改动前 _index_new 只处理新增 → Chroma 永远是旧文本，而 BM25 从 DB 重建 → split-brain")

    # P3：只涨源数、正文没变 → 不该重建 BM25
    db = fresh_db()
    old = add_news(db, "标题", "长" * 80, pt=now - timedelta(hours=1))
    coll = FakeCollection([(old.id, vec)])
    rebuilds: list[int] = []
    classify, _ = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(coll),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: rebuilds.append(1),
                 _upsert_index=lambda *a, **k: None):
        DA.run_data_agent(db, raw=[item("标题", "短", pt=now)])
    check("P3：只合并、正文没变 → 不重建 BM25", rebuilds, [],
          "BM25 metadata 只存 news_id，正文没变就没必要全量重建（2.2s/轮）")

    # P3：有新增 → 必须重建
    db = fresh_db()
    rebuilds2: list[int] = []
    classify, _ = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(FakeCollection()),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: rebuilds2.append(1),
                 _upsert_index=lambda *a, **k: None):
        DA.run_data_agent(db, raw=[item("全新", "全新正文", pt=now)])
    check("P3：有新增 → 重建 BM25", len(rebuilds2), 1)

    # 灰区 fail-open：LLM 调用失败（返回 None）→ 按新条目入库并计数
    reset_vectors()
    db = fresh_db()
    register("灰区事件", "灰区正文", vec)   # 候选 → 与下面 part 的余弦 = 0.80，落在灰区
    part = unit(0.8, 0.6)
    old = add_news(db, "灰区事件", "灰区正文", pt=now - timedelta(hours=1))
    coll = FakeCollection([(old.id, part)])
    classify, calls = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(coll),
                 classify_news=classify, _llm_is_duplicate=lambda *a: None,
                 _rebuild_bm25=lambda *a, **k: None, _upsert_index=lambda *a, **k: None):
        res = DA.run_data_agent(db, raw=[item("灰区事件", "灰区正文", pt=now)])
    check("灰区 LLM 失败 → fail-open：按新条目入库", res["new"], 1,
          "宁可多一条重复，也不要把真新闻当重复吞掉 —— 后者不可逆")
    check("灰区 LLM 失败 → 被如实计数", res["gray_check_failed"], 1,
          "改动前这里是裸的 except: return False，连日志都没有")

    # ⚠️ 上面测的是**调用方**怎么处理 None —— 但 `_llm_is_duplicate` 是被 stub 掉的，
    # 所以「真函数到底会不会返回 None」根本没被测到。变异测试正是从这个缝里钻过去的。
    # 这里补上：让真正的 LLM 抛异常，看它返回什么。
    class _BoomLLM:
        def invoke(self, *a, **k):
            raise RuntimeError("模拟 LLM 挂掉")

    with patched(get_llm=lambda **k: _BoomLLM()):
        got = DA._llm_is_duplicate(News(title="A", source="s"),
                                   DA.Candidate(item=item("B", "b")))
    check("真实 _llm_is_duplicate：LLM 抛异常 → 返回 None（而不是 False）", got, None,
          "三态是「失败」与「判为新」能被区分开的前提")

    # 判重请求的**输入形态**（2026-09-23 实机发现）：改动前 A 只给标题、
    # B 给「标题 + 换行 + 正文前 150 字」—— 两侧信息不对等，且 B 里的换行
    # 会把 DEDUP_PROMPT 的结构撑开（「新闻B：」那行之后多出一段游离文本）。
    captured: dict = {}

    class _CaptureLLM:
        def invoke(self, prompt):
            captured["p"] = prompt
            return type("R", (), {"content": "false"})()

    # ⚠️ 正文里**必须带换行** —— 否则 `_fmt_for_dedup` 的空白折叠无从体现，
    # 去掉它也不会有区别，下面那条断言就成了**假通过**（本脚本第一版就是这么错的）。
    with patched(get_llm=lambda **k: _CaptureLLM()):
        DA._llm_is_duplicate(
            News(title="A标题", content="A的正文第一行\nA的正文第二行",
                 source="新浪财经"),
            DA.Candidate(item=item("B标题", "B的正文第一行\nB的正文第二行",
                                   source="东方财富")))
    _p = captured["p"]
    check("判重 prompt 两侧**同形态**（都带正文摘要）",
          "A标题｜" in _p and "B标题｜" in _p, True,
          "一侧只有标题、一侧是标题+正文 → 拿不对等的输入让 LLM 做判断")
    check("判重 prompt 的新闻行内**不含换行**（换行会撑破结构）",
          len(_p.strip().splitlines()), 4,
          "行数应为 4：说明行 / 空行 / 新闻A / 新闻B —— 多出来就是正文里的换行"
          "把「新闻B：」那行撑成了两段游离文本")


def _within_round_in_pipeline():
    print("\n[9] 本轮内聚类必须真的接在主流程上")

    reset_vectors()
    db = fresh_db()
    # 两条向量相近（cos 0.99）、库里空 → 应在本轮内合成一个候选，只插 1 行
    register("同事件A", "正文A", unit(1, 0))
    register("同事件B", "正文B", unit(0.99, 0.14))
    classify, calls = make_classifier()
    # 数 embed_documents 的**调用次数与批量大小** —— M3 就是靠这个验的：
    # 簇合并后若重新嵌入，这里会多出一次调用（2026-09-23 实机该步 22s）。
    embed_batches: list[int] = []

    def _counting_embed(texts):
        embed_batches.append(len(texts))
        return fake_embed(texts)

    with patched(embed_documents=_counting_embed,
                 vector_store=FakeStore(FakeCollection()),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: None, _upsert_index=lambda *a, **k: None):
        res = DA.run_data_agent(db, raw=[item("同事件A", "正文A"), item("同事件B", "正文B")])
    check("本轮内两条同事件 → 只插 1 行", res["new"], 1,
          "直接调 _cluster_by_similarity 测不到「主流程有没有用它」")
    check("本轮内两条同事件 → within_round_merged = 1", res["within_round_merged"], 1)
    check("本轮内两条同事件 → 只送 1 条进分类", calls, [["同事件A"]])
    check("**簇合并后不重新嵌入**（只调一次 embed_documents）", embed_batches, [2],
          "合并后候选的 item 就是簇代表，文本未变 → vecs[rep] 直接可用。"
          "重算一次是纯冗余（M3，实测该步 22s 里含一次全量重算）")


def _duplicate_merge_target():
    print("\n[10] 同一行被多个候选合并（Chroma 重复 id）")

    reset_vectors()
    now = datetime.now()
    # ⚠️ 两个候选的向量**必须彼此不像**（cos 0.6 < 0.85），否则会先被本轮内聚类
    # 并成一个候选、根本走不到「两个候选并进同一行」那条路径。
    # 但两者都要像库内那一行（cos ≈ 0.894 ≥ 0.85）。
    vec = unit(1, 0)
    register("同事件A", "长" * 80, unit(1, 0.5))
    register("同事件B", "长" * 90, unit(1, -0.5))

    # ① `_upsert_index` 自己对重复 id 兜底
    db = fresh_db()
    a = add_news(db, "A", "a")
    b = add_news(db, "B", "b")
    store = FakeUpsertStore()
    # ⚠️ 必须接住异常：断言不过时 `FakeUpsertStore` 会抛，不接的话整个脚本当场崩掉、
    # 后面几组都跑不到 —— 看到的是一条 traceback 而不是一条 FAIL。
    try:
        with patched(embed_documents=fake_embed, vector_store=store):
            DA._upsert_index([a, a, b])
        got: object = store.calls
    except Exception as e:  # noqa: BLE001
        print(f"        · _upsert_index 抛 {type(e).__name__}: {e}")
        got = None
    check("_upsert_index 对重复 id 去重", got, [[a.id, b.id]],
          "真实 Chroma 对重复 id 抛 DuplicateIDError，而这个异常在 db.commit() **之后** —— "
          "整轮会以「数据已提交、索引没更新」的半完成状态收场")

    # ② 主流程：两个候选的最近邻都指向同一行 → changed 不该有重复
    db = fresh_db()
    old = add_news(db, "同一事件", "短", pt=now - timedelta(hours=1))
    coll = FakeCollection([(old.id, vec)])
    indexed: list[list[str]] = []
    classify, _ = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(coll),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: None,
                 _upsert_index=lambda rows: indexed.append([r.id for r in rows])):
        res = DA.run_data_agent(db, raw=[item("同事件A", "长" * 80, pt=now),
                                         item("同事件B", "长" * 90, pt=now)])
    check("两个候选并进同一行 → upsert 列表里该 id 只出现一次", indexed, [[old.id]])
    check("两个候选并进同一行 → merged = 2", res["merged"], 2)


class _Sink:
    """loguru 捕获器 —— 这些断言测的是**日志本身**，不抓就无从断言。"""

    def __init__(self):
        self.lines: list[str] = []

    def write(self, msg):
        self.lines.append(str(msg))

    @property
    def text(self) -> str:
        return "".join(self.lines)


def _log_contracts():
    print("\n[11] 日志口径（第 1 批可观测性）")

    # ---- ① [去重] 汇总口径：真实省下的量，而不是只报「库内已有」那一段 ----
    reset_vectors()
    now = datetime.now()
    vec = unit(1, 0)
    register("已在库", "正文", vec)
    db = fresh_db()
    old = add_news(db, "已在库", "正文", pt=now - timedelta(hours=1))
    coll = FakeCollection([(old.id, vec)])
    classify, _ = make_classifier()
    sink = _Sink()
    hid = logger.add(sink.write, level="INFO", format="{message}")
    try:
        with patched(embed_documents=fake_embed, vector_store=FakeStore(coll),
                     classify_news=classify, _llm_is_duplicate=lambda *a: False,
                     _rebuild_bm25=lambda *a, **k: None, _upsert_index=lambda *a, **k: None):
            res = DA.run_data_agent(db, raw=[
                item("已在库", "正文", pt=now),          # → 并入已有
                item("全新", "全新正文", pt=now),         # → 分类
                item("重复A", "重复内容", pt=now),        # ┐ 同指纹
                item("重复A", "重复内容", pt=now),        # ┘ → 确定性去重 1 条
            ])
    finally:
        logger.remove(hid)

    # 用「候选数 − 已并入数」求「送进分类的量」，**不要用 `res["classified"]`**：
    # 后者是**分类成功**的条数，一旦出现分类失败/漏项就小于实际送进去的量，
    # 断言会假通过（当前 stub 分类器恒成功，二者恰好相等，掩盖了这个差别）。
    saved = res["collected"] - (res["candidates"] - res["merged"])
    check("**[去重] 行报的是「共省下」，等于 采集数 − 待分类数**",
          f"共省下 {saved} 个候选" in sink.text, True,
          "改动前只印 len(to_merge)，**低估约 8 倍** —— 读日志的人得自己把三行相加")
    check("且拆开列出三段贡献（确定性 / 本轮内 / 库内）",
          all(k in sink.text for k in ("确定性去重", "本轮内语义合并", "库内已有")), True)
    check("语义比对那 86 秒的黑盒已拆成三段耗时",
          "Chroma 批查" in sink.text and "DB 取行" in sink.text, True)

    # ---- ② [索引] Chroma 写入日志（改动前**完全无日志**）----
    db2 = fresh_db()
    a = add_news(db2, "A", "a")
    store = FakeUpsertStore()
    sink2 = _Sink()
    hid = logger.add(sink2.write, level="INFO", format="{message}")
    try:
        with patched(embed_documents=fake_embed, vector_store=store):
            DA._upsert_index([a])
    finally:
        logger.remove(hid)
    check("Chroma upsert 有条数与耗时日志",
          "Chroma upsert 1 条" in sink2.text and "向量化" in sink2.text, True,
          "改动前这里一行日志都没有，P2 的修复效果只能靠 DB 反推")

    # ---- ③ [分类] 每批带「成功 / 漏项 / 模型」----
    class _FakeLLM:
        def invoke(self, prompt):
            ids = re.findall(r"^(\d+)\. ", prompt, re.M)
            data = [{"id": int(i), "relevant": True, "category": "综合",
                     "market": "无", "themes": []} for i in ids]
            return type("R", (), {"content": json.dumps(data, ensure_ascii=False)})()

    sink3 = _Sink()
    hid = logger.add(sink3.write, level="INFO", format="{message}")
    try:
        with patched(get_llm=lambda **k: _FakeLLM()):
            DA.classify_news([item(f"T{i}") for i in range(3)])
    finally:
        logger.remove(hid)
    check("分类日志含条数与模型名（原先只有「第 N/M 批」，看不出走了没走备用）",
          "第 1/1 批" in sink3.text and "成功 3" in sink3.text
          and "漏项 0" in sink3.text and "模型" in sink3.text, True)

    # ---- ④ 重复 id 不能让「漏项」变成负数 ----
    class _DupLLM:
        """故意返回重复 id —— `batch_ok` 会照收多条，`covered` 才是去重集合。"""

        def invoke(self, prompt):
            one = {"id": 1, "relevant": True, "category": "综合",
                   "market": "无", "themes": []}
            return type("R", (), {"content": json.dumps([one] * 3, ensure_ascii=False)})()

    sink4 = _Sink()
    hid = logger.add(sink4.write, level="INFO", format="{message}")
    try:
        with patched(get_llm=lambda **k: _DupLLM()):
            DA.classify_news([item("T0"), item("T1")])
    finally:
        logger.remove(hid)
    check("LLM 返回重复 id → 「漏项」不为负，且与同批 warning 一致",
          "漏项 1" in sink4.text and "漏项 -1" not in sink4.text, True,
          "按 len(batch_ok) 算会得「成功 3 / 漏项 -1」（批只有 2 条）—— "
          "巡检已复现；也与那条 len(missed)/len(batch) 的 warning 自相矛盾")


def _result_shape():
    print("\n[8] 返回值契约")
    db = fresh_db()
    classify, _ = make_classifier()
    with patched(embed_documents=fake_embed, vector_store=FakeStore(FakeCollection()),
                 classify_news=classify, _llm_is_duplicate=lambda *a: False,
                 _rebuild_bm25=lambda *a, **k: None, _upsert_index=lambda *a, **k: None):
        res = DA.run_data_agent(db, raw=[])
    need = {"collected", "classified", "relevant", "dropped", "classify_failed",
            "new", "merged"}
    check("既有键一个不少（调用方依赖它们）", need <= set(res), True)
    check("空输入不炸", (res["collected"], res["new"]), (0, 0))


# ============ ⑫ B1：失败分类 / 抖动退避 / 熔断 ============
class _BoomLLM:
    """一调就抛的假 LLM —— 复现「LLM 不可用」那一轮（2026-09-22：0.43 秒刷 110 条）。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def invoke(self, _prompt):
        raise self.exc


class _FakeStatus(Exception):
    """带 `status_code` 的假异常 —— `app.retry` 的判据优先看状态码，不看消息文本。"""

    def __init__(self, code: int) -> None:
        super().__init__(f"Error code: {code} - fake")
        self.status_code = code


def _classification_failure():
    print("\n[12] B1：失败分类 / 抖动退避 / 熔断（2026-09-24）")

    # ---- ① 退避函数本身：下界固定、上界指数放大且被 cap 截断 ----
    seen: list[tuple[float, float]] = []
    orig_uniform = DA.random.uniform
    DA.random.uniform = lambda a, b: (seen.append((a, b)), (a + b) / 2)[1]
    try:
        for att in (1, 2, 9):
            DA._backoff_delay(att)
    finally:
        DA.random.uniform = orig_uniform
    check("下界恒为 base，上界按 3 倍指数放大并被 cap 截断",
          seen, [(1.0, 3.0), (1.0, 8.0), (1.0, 8.0)],
          "AWS decorrelated jitter 形式。**抖动不能省** —— 写死的 2/4/8 会让多个客户端"
          "同步重打（业界量化：抖动可减少 60~80% 的重试风暴）")
    spread = {DA._backoff_delay(2) for _ in range(20)}
    check("区间内真随机（20 次取值不全相同）", len(spread) > 1, True)
    check("取值恒落在 [base, cap]",
          all(DA.BACKOFF_BASE <= d <= DA.BACKOFF_CAP for d in spread), True)

    # ---- ② 失败分类**复用 `app.retry.is_retryable`**，不另写一份 ----
    from app.retry import is_retryable  # noqa: PLC0415
    check("连接错误 → 可重试", is_retryable(ConnectionError("Connection error")), True)
    check("429 限流 → 可重试", is_retryable(_FakeStatus(429)), True)
    check("401 鉴权失败 → **不可重试**", is_retryable(_FakeStatus(401)), False,
          "退避对 terminal 错误是纯白等 —— 换谁都不会好。判据**按状态码与异常类型**，"
          "不匹配消息文本（文本里常含无关数字，会误伤 500/超时）")

    # ---- ③ 循环里的实际行为：100 条 = 5 批，第 3 批熔断 ----
    items = [item(f"t{i}") for i in range(100)]
    sleeps: list[float] = []
    sink = _Sink()
    hid = logger.add(sink.write, level="INFO", format="{message}")
    orig_sleep = DA.time.sleep            # `DA.time` 就是 time 模块本身，用完必须还原
    try:
        DA.time.sleep = sleeps.append
        try:
            with patched(get_llm=lambda **kw: _BoomLLM(ConnectionError("Connection error")),
                         llm_retry_times=lambda **kw: 1):
                results, failed = DA.classify_news(items)
        finally:
            DA.time.sleep = orig_sleep
    finally:
        logger.remove(hid)

    lines = sink.text.splitlines()
    batch_lines = [ln for ln in lines if "[分类] 第" in ln]
    check("连续 3 批即熔断：只打了 3 条批日志（不是 5 条）",
          len(batch_lines), DA.MAX_CONSECUTIVE_BATCH_FAILS,
          "2026-09-22 实测：连接中断后 **0.433 秒内刷完 55 批**（3.9 毫秒/批），"
          "循环里没有任何等待 —— 打 55 次和打 1 次没区别，只是把日志刷爆")
    check("熔断时剩余条目**一并交出**（100 条全计入分类失败）", len(failed), 100,
          "不交就会出现「既不在 results 也不在 failed」→ classify_failed=0 "
          "→ 水位线照常推进 → 那批新闻**永久丢失**")
    check("成功后无残留", results, [])
    check("熔断行说清了「水位线不推进，下轮重取」",
          "中断本轮分类" in sink.text and "水位线不推进" in sink.text, True)
    check("退避真的执行了，且**跳过最后一次**（要中断了还等没有意义）",
          len(sleeps), 2)
    check("每次退避都在 [base, cap] 内",
          all(DA.BACKOFF_BASE <= s <= DA.BACKOFF_CAP for s in sleeps), True)
    check("全轮日志被压到 8 行以内（对照事故的 110 行）",
          len([ln for ln in lines if "[分类]" in ln or "[重试]" in ln]) <= 8, True,
          "文档 §8.4 的验收标准。⚠️ **该上限有前提**：本断言用 "
          "`llm_retry_times=1`（= 配了备用模型时 `news` 分组的实际取值，也正是 "
          "2026-09-22 那次事故的取值）。**未配备用时 `llm_retry_times=3`** → "
          "`app/retry.py` 每失败批会吐 2 条 WARNING + 1 条 ERROR，3 批约 13 行，"
          "会超过 10 行 —— 那里是采集层共用的代码，本次未动（巡检 2026-09-24 指出）")

    # ---- ④ terminal 错误：不退避（白等）----
    # 这一段只关心「睡没睡」，不关心日志文本 —— 整段静音，免得 8 行原始
    # ERROR 混进断言输出里，让读的人以为脚本自己报错了。
    sleeps2: list[float] = []
    orig_sleep2 = DA.time.sleep
    logger.disable("")
    try:
        DA.time.sleep = sleeps2.append
        try:
            with patched(get_llm=lambda **kw: _BoomLLM(_FakeStatus(401)),
                         llm_retry_times=lambda **kw: 1):
                DA.classify_news([item(f"u{i}") for i in range(100)])
        finally:
            DA.time.sleep = orig_sleep2
    finally:
        logger.enable("")
    check("401（terminal）→ **一次都没退避**", sleeps2, [],
          "对鉴权/参数类错误等待是纯白等；它们仍会按 3 批熔断，"
          "所以日志量并不因此变多")

def main() -> int:
    _fingerprint()
    _group_by_fingerprint()
    _cluster()
    _time_window()
    _merge_and_insert()
    _pipeline_order()
    _p2_p3_failopen()
    _within_round_in_pipeline()
    _duplicate_merge_target()
    _log_contracts()
    _result_shape()
    _classification_failure()
    failed = [n for ok, n in _RESULTS if not ok]
    print(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
