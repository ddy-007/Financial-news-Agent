"""新闻业务：采集 + 去重入库 + 向量索引。"""
from __future__ import annotations

import threading
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import func
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
from app.services.info_level import is_round_overdue
from app.services.news_archive import coverage_counts, pending_items, save_capture


# ================= 增量水位线 =================
def load_states(db: Session) -> dict[str, Anchor]:
    """读取各源原文抓取水位；迁移前回退旧处理水位，返回 `{源名: Anchor}`。

    表为空（首次运行）或缺某个源时**不报错** —— 缺锚点的源会在采集层自动
    回退成「距现在 `news_lookback_days` 天」。
    """
    return {
        s.source: Anchor(last_ts=max(t for t in (s.fetched_ts, s.last_ts) if t is not None)
                        if s.fetched_ts or s.last_ts else None,
                         last_id=s.last_id if s.fetched_ts is None else None)
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
                classify_failed: int, *, processing_pending: int = 0) -> None:
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
        if processing_pending > 0:
            logger.warning(f"[采集] 尚有 {processing_pending} 条原文待处理，"
                           f"{r.source} 处理水位线不推进；抓取进度已独立保存")
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
                # ⚠️ 区间必须写 [{prev}, {oldest})：prev 是**较早**的水印、
                # oldest 是**较晚**的「本轮取到的最旧一条」—— 缺口就夹在中间。
                # 2026-09-23 修：原写作 [{oldest}, {prev})，起止颠倒，读者按字面
                # 会得出「9/23 06:44 到 9/22 22:29 之间丢了新闻」这种矛盾区间。
                # （首次运行时 prev=None，恰好落在右端，掩盖了这个 bug。）
                f"本轮取到的最旧一条 {oldest}，区间 [{prev}, {oldest}) 的新闻本轮"
                f"**未取到**。历史覆盖缺口独立保留，请用 scripts/backfill_news.py 预览区间补采；"
                f"普通增量成功不代表历史缺口已补齐"
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
# **为什么需要**：采集有两个触发方 —— APScheduler 的定时 job（间隔见
# `NEWS_INTERVAL_MINUTES`，2026-09-22 起为 60 分钟整点），以及
# `POST /api/v1/news/collect` 的手动端点。定时 job 有 APScheduler 的
# `max_instances=1` 兜着，**手动端点完全没有保护**。
#
# 2026-09-22 实机撞上：手动触发的一轮（21:33 起）要跑 45 分钟，而定时的那轮
# 22:00 照常起来 —— **两轮重叠**：LLM 调用数翻倍（220 批）、两轮并发写同一个
# SQLite、还会竞争同一份水位线。当天没出事（第二轮 1120 条分类失败，
# `save_states` 规则 2 挡住了水位线推进），但那是运气。
#
# 注意它只挡**同一进程内**的并发；多进程部署需要换成文件锁或 DB 锁。
_COLLECT_LOCK = threading.Lock()


def _try_process_lock():
    """普通采集与独立补采脚本共用文件锁；进程退出由操作系统释放。"""
    path = Path(__file__).resolve().parents[2] / "data" / "news_ingest.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.seek(0, 2) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


@contextmanager
def collection_guard():
    """CLI 与后台共用互斥；获取失败不排队。"""
    if not _COLLECT_LOCK.acquire(blocking=False):
        yield False
        return
    handle = None
    try:
        handle = _try_process_lock()
        yield handle is not None
    finally:
        if handle is not None:
            handle.close()
        _COLLECT_LOCK.release()


def news_readiness(db: Session, *, now: datetime | None = None) -> dict:
    """新闻轮的**就绪状态** —— 研判取数前判断「本窗口的新闻是否已落地」（H1，2026-09-24）。

    **为什么需要它**：生产者（采集轮）与消费者（研判）之间原本**只有时间对齐**，
    没有任何就绪信号。2026-09-23 实测：新闻轮 18:00 起跑、18:21:08 才入库，
    而研判 18:15 取数 —— 缺口 6 分 9 秒，**当天的新闻一条都没进日报**，
    报告却照事实写出「新增 0 条」，再把它解读成「今日无重大消息」。

    **数据来源复用 `collector_state.last_ok_at`，不新增存储**：它只在
    「采集完整 + 无分类失败 + 确有推进」时才更新（见 `save_states` 的规则表），
    语义恰好就是「上一轮成功落地」。—— 取**所有源的最大值**，回答的是
    「整轮有没有成功跑完」；单个源失效由 `collector_health` 负责告警，不在这里重复判。

    ⚠️ 已知的**过报**（有意选择安全侧）：分类失败时水位线不推进 → `last_ok_at`
    停在上一轮 → 这里会报「未落地」。而那一轮其实有部分条目入库了。宁可说
    「本窗口数据可能不全」，也不要说「数据是新的」——H1 的教训正在这里。
    """
    now = now or datetime.now()
    last = db.query(func.max(CollectorState.last_ok_at)).scalar()
    secs = (now - last).total_seconds() if last else None
    return {
        "last_round_at": last,
        "seconds_since": secs,
        "collecting": _COLLECT_LOCK.locked(),
        "overdue": is_round_overdue(secs, settings.news_interval_minutes),
        **coverage_counts(db),
    }


def run_collection(db: Session) -> tuple[list[NewsItem], list[SourceResult]]:
    """读取水位线 → 采集全部源 → 返回 (全部条目, 各源结果)。

    先原子保存原文、抓取进度和历史缺口，再交给后续处理。
    处理水位仍由 `save_states` 在处理完成后推进，抓取不再受模型失败牵制。
    """
    anchors = load_states(db)
    logger.info(f"[采集] 水位线：{ {k: str(v.last_ts) for k, v in anchors.items()} }")
    results = collect_all_news(anchors)
    save_capture(db, results, anchors)
    items = [it for r in results for it in r.items]
    return items, results


def collect_and_store_news(db: Session) -> int:
    """采集 + 过滤 + 去重合并 + 索引（走数据采集 Agent），返回新增条数。

    「采集 → 原文/抓取进度落库 → 处理待办 → 入库/索引 → 处理水位」。
    原文与抓取水位同事务，后续失败由持久化队列重试，不依赖源站再次保留新闻。

    **同一时刻只允许一次采集**（见 `_COLLECT_LOCK`）。拿不到锁就跳过并返回 0 ——
    调用方靠日志区分"跳过了"与"真的没新增"。
    """
    from app.agent.data_agent import run_data_agent

    # 非阻塞：拿不到说明已有一次采集在跑，直接跳过。
    # 不用阻塞等待 —— 一轮要几十分钟，等它对调用方毫无意义。
    if not _COLLECT_LOCK.acquire(blocking=False):
        logger.warning(
            "[采集] 已有一次采集在进行中，本次**跳过**（不排队）。"
            "定时任务按 NEWS_INTERVAL_MINUTES 触发（默认 60 分钟），"
            "而一轮可能跑更久，两者会撞上"
        )
        return 0
    process_lock = None
    try:
        process_lock = _try_process_lock()
        if process_lock is None:
            logger.warning("[采集] 另一个进程正在采集或补采，本次跳过")
            return 0
        items, results = run_collection(db)
        # run_collection 已将原文与抓取进度原子保存；处理队列跨进程保留失败条目。
        res = run_data_agent(db, raw=pending_items(db))
        remaining = coverage_counts(db)["pending_count"]
        # 旧 last_ok_at 继续表示处理完成；fetched_ts 独立表示已保存原文。
        save_states(db, results, res.get("classify_failed", 0), processing_pending=remaining)
        # P3 源健康巡检。**必须带本轮上下文**（results / classify_failed）——
        # 只看 collector_state 的话，「LLM 故障导致水位线不推进」会被误报成
        # 「源失效」，把排查引向错误方向（见 collector_health 的模块 docstring）。
        #
        # ⚠️ 自己接住异常：此刻新闻**已经入库、水位线已经推进**，巡检抛错会让
        # 整轮对外表现为失败（定时任务记异常、手动端点返 500），与事实不符。
        # 巡检是观测手段，不该有推翻主流程的权限。
        try:
            from app.services.collector_health import evaluate, log_health
            log_health(evaluate(db, results=results,
                                classify_failed=res.get("classify_failed", 0)))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[源健康] 巡检失败（不影响本轮采集结果）: {e}")
        return res["new"]
    except Exception as exc:
        db.rollback()
        # 写入 loguru 文件 sink；只输出堆栈帧，避免诊断器展开 SQL 参数和局部变量。
        import traceback
        from sqlalchemy.exc import SQLAlchemyError
        from app.retry import describe_exception

        detail = (describe_exception(exc.orig) if isinstance(exc, SQLAlchemyError)
                  and getattr(exc, "orig", None) is not None
                  else describe_exception(exc))
        frames = "".join(traceback.format_tb(exc.__traceback__))
        logger.error(f"[采集] 处理失败，处理水位线未推进；若原文已提交则保留抓取进度待重试：{detail}\n{frames}")
        raise
    finally:
        if process_lock is not None:
            process_lock.close()
        _COLLECT_LOCK.release()
