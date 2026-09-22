"""新闻业务：采集 + 去重入库 + 向量索引。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.collectors.news_collector import (
    Anchor, NewsItem, SourceResult, collect_all_news,
)
from app.config import settings
from app.models.collector_state import CollectorState
from app.models.news import News
from app.rag import vector_store
from app.rag.embeddings import embed_documents
from app.rag.retriever import get_retriever


# ================= 增量水位线 =================
def load_states(db: Session) -> dict[str, Anchor]:
    """读取各源水位线，返回 `{源名: Anchor}`。

    表为空（首次运行）或缺某个源时**不报错** —— 缺锚点的源会在采集层自动
    回退成「距现在 `news_lookback_days` 天」。
    """
    return {
        s.source: Anchor(last_ts=s.last_ts, last_id=s.last_id)
        for s in db.query(CollectorState).all()
    }


def _newest(result: SourceResult) -> tuple[datetime | None, str | None]:
    """本轮该源取到的**最新**发布时间及其标识（空则 None）。"""
    candidates = [i for i in result.items if i.publish_time]
    if not candidates:
        return None, None
    newest = max(candidates, key=lambda i: i.publish_time)
    return newest.publish_time, (newest.url or None)


def _oldest(result: SourceResult) -> datetime | None:
    """本轮该源取到的**最旧**发布时间（用于把截断缺口的区间写清楚）。"""
    ts = [i.publish_time for i in result.items if i.publish_time]
    return min(ts) if ts else None


def save_states(db: Session, results: list[SourceResult],
                classify_failed: int) -> None:
    """按设计 §11 的规则表推进各源水位线。**必须在链路全部成功后调用。**

    规则（按序判定，先命中先决定）：

    | # | 条件 | 是否推进 |
    |:--:|---|:--:|
    | 1 | 该源 `ok=False`（有失败页） | ❌ 下轮重取同窗口 |
    | 2 | 本轮**存在分类失败** | ❌ **全部源**都不推进 —— 条目没入库，推进即永久丢失 |
    | 3 | 该源撞页上限 | ✅ 推进（显式例外，否则会死锁），并落 `truncated_at` + 告警 |
    | 4 | 其余 | ✅ 推进 |

    > 规则 2 **抢先于**规则 3：同一轮既撞上限又分类失败时走规则 2（不推进）。
    > 这不是死锁 —— 分类一恢复，规则 2 不再命中，规则 3 生效即自动解开。
    """
    now = datetime.now()
    for r in results:
        st = db.get(CollectorState, r.source)
        if st is None:
            st = CollectorState(source=r.source, empty_streak=0)
            db.add(st)
        st.updated_at = now

        # 规则 1：采集不完整 → 不推进。
        # 页是新→旧顺序的，中间某页失败意味着水位线里会留一个**中段空洞**，
        # 推进的话下一轮只取最新那段，空洞永不回补。
        if not r.ok:
            logger.warning(f"[采集] {r.source} 本轮不完整（失败 {r.failed_pages} 页），"
                           f"水位线**不推进**，下轮重取同窗口")
            continue

        # 规则 2：有分类失败 → 全部源都不推进。
        if classify_failed > 0:
            logger.warning(f"[采集] 本轮有 {classify_failed} 条分类失败，"
                           f"条目未入库 → {r.source} 水位线**不推进**，下轮重取")
            continue

        latest, latest_id = _newest(r)
        if latest is None:
            # 成功但 0 条：不推进，累计连续空轮（P3 监控靠它发现"源改版了"）
            st.empty_streak = (st.empty_streak or 0) + 1
            logger.info(f"[采集] {r.source} 本轮 0 条，empty_streak={st.empty_streak}")
            continue

        # 规则 3 / 4：推进。
        prev, oldest = st.last_ts, _oldest(r)
        st.last_ts = latest
        st.last_id = latest_id
        st.last_ok_at = now
        st.empty_streak = 0
        st.truncated_at = now if r.truncated else None

        if r.truncated:
            # 缺口必须写清区间 —— 只记"被截断"看不出丢了哪一段，事后无从判断影响面。
            logger.warning(
                f"[采集] {r.source} 本轮被页上限截断：上一水印 {prev}，"
                f"本轮取到的最旧一条 {oldest}，区间 [{oldest}, {prev}) 的新闻本轮"
                f"**未取到**。跑 scripts/catchup.py 可补回约 8~10 小时以内的区段"
                f"（它同样受 {settings.news_max_pages} 页上限）；更早的那段补不回来"
            )
    db.commit()


def _item_to_news(item: NewsItem) -> News:
    return News(
        title=item.title,
        content=item.content or "",
        source=item.source,
        url=item.url or None,  # 空字符串转 None，避免 UNIQUE 冲突
        publish_time=item.publish_time or datetime.now(),
        category=item.category,
    )


def save_news_items(db: Session, items: list[NewsItem]) -> list[News]:
    """去重入库（含批次内去重），返回新增的 News 对象。"""
    new_news: list[News] = []
    # 一次性取出现有 url，避免逐条查询
    existing = {u for (u,) in db.query(News.url).all() if u}
    seen: set[str] = set()
    for item in items:
        if not item.title:
            continue
        if item.url:
            if item.url in existing or item.url in seen:
                continue  # 库内或批次内重复
            seen.add(item.url)
        n = _item_to_news(item)
        db.add(n)
        new_news.append(n)
    db.commit()
    return new_news


def index_news_batch(news_list: list[News]) -> None:
    """将新闻写入 ChromaDB（dense 向量）。"""
    if not news_list:
        return
    ids = [n.id for n in news_list]
    texts = [f"{n.title}\n{n.content}" for n in news_list]
    embeddings = embed_documents(texts)
    metadatas = [
        {
            "news_id": n.id,
            "title": n.title,
            "source": n.source,
            "publish_time": n.publish_time.isoformat() if n.publish_time else "",
            "category": n.category or "",
        }
        for n in news_list
    ]
    vector_store.upsert_documents(ids, texts, embeddings, metadatas)


def rebuild_bm25_index(db: Session, days: int = 7) -> None:
    """从 DB 读取近期新闻，重建内存 BM25 索引。"""
    since = datetime.now() - timedelta(days=days)
    news_list = db.query(News).filter(News.publish_time >= since).all()
    chunks = [
        {"id": n.id, "text": f"{n.title}\n{n.content}", "metadata": {"news_id": n.id}}
        for n in news_list
    ]
    get_retriever().build_bm25_index(chunks)


# 采集互斥锁（**进程级**）。
#
# **为什么需要**：采集有两个触发方 —— APScheduler 的定时 job（每 30 分钟），
# 以及 `POST /api/v1/news/collect` 的手动端点。定时 job 有 APScheduler 的
# `max_instances=1` 兜着，**手动端点完全没有保护**。
#
# 2026-09-22 实机撞上：手动触发的一轮（21:33 起）要跑 45 分钟，而定时的那轮
# 22:00 照常起来 —— **两轮重叠**：LLM 调用数翻倍（220 批）、两轮并发写同一个
# SQLite、还会竞争同一份水位线。当天没出事（第二轮 1120 条分类失败，
# `save_states` 规则 2 挡住了水位线推进），但那是运气。
#
# 注意它只挡**同一进程内**的并发；多进程部署需要换成文件锁或 DB 锁。
_COLLECT_LOCK = threading.Lock()


def run_collection(db: Session) -> tuple[list[NewsItem], list[SourceResult]]:
    """读取水位线 → 采集全部源 → 返回 (全部条目, 各源结果)。

    **刻意不在这里写水位线**：此刻条目还只是一堆内存对象，分类与入库都没发生。
    推进水位线的唯一时机是 `save_states`，而它由 `collect_and_store_news`
    在整条链路跑完之后才调用 —— 这个顺序是"不丢新闻"的最后一道保险。
    """
    anchors = load_states(db)
    logger.info(f"[采集] 水位线：{ {k: str(v.last_ts) for k, v in anchors.items()} }")
    results = collect_all_news(anchors)
    items = [it for r in results for it in r.items]
    return items, results


def collect_and_store_news(db: Session) -> int:
    """采集 + 过滤 + 去重合并 + 索引（走数据采集 Agent），返回新增条数。

    **水位线在最后一步才推进**（见 `save_states`）—— 顺序是
    「采集 → 分类 → 入库 → 才推进水印」，任何一环不完整都不推进。
    这个顺序是整条链路里最要紧的一处：提前推进 = 那批新闻还没入库就把
    水位线划过去，下一轮不会再取，**永久丢失**。

    **同一时刻只允许一次采集**（见 `_COLLECT_LOCK`）。拿不到锁就跳过并返回 0 ——
    调用方靠日志区分"跳过了"与"真的没新增"。
    """
    from app.agent.data_agent import run_data_agent

    # 非阻塞：拿不到说明已有一次采集在跑，直接跳过。
    # 不用阻塞等待 —— 一轮要几十分钟，等它对调用方毫无意义。
    if not _COLLECT_LOCK.acquire(blocking=False):
        logger.warning(
            "[采集] 已有一次采集在进行中，本次**跳过**（不排队）。"
            "定时任务每 30 分钟触发，而一轮可能跑更久，两者会撞上"
        )
        return 0
    try:
        items, results = run_collection(db)
        res = run_data_agent(db, raw=items)
        save_states(db, results, res.get("classify_failed", 0))
        return res["new"]
    finally:
        _COLLECT_LOCK.release()
