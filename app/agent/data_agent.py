"""数据采集 Agent（混合 Pipeline）。

流程：采集 → LLM 相关性分类(过滤) → 向量语义去重 + LLM 复核 → 多源合并 → 入库索引。

定位：LLM 只做语义判断（相关性、灰色复核），向量/代码做确定性计算（相似度、合并字段）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.agent.llm import get_llm, llm_retry_times
from app.agent.prompts import CLASSIFY_PROMPT, DEDUP_PROMPT
from app.collectors.news_collector import NewsItem, collect_all_news
from app.models.news import News
from app.retry import call_with_retry
from app.rag import vector_store
from app.rag.embeddings import embed_documents, embed_query
from app.rag.retriever import get_retriever

BATCH_SIZE = 20       # LLM 批量分类每批条数
DUP_THRESHOLD = 0.85  # 相似度 ≥ 此值判为重复
GRAY_LOW = 0.70       # 相似度在此区间则交 LLM 复核
BATCH_INDEX_SIZE = 50  # 每处理 N 条 commit+索引一次，使批次内跨源重复可被检出

# 分类标签归一化（LLM 输出可能漂移到预设列表之外）
# 8 类：宏观/政策/行业/公司/国际/资金/市场/综合
ALLOWED_CATEGORIES = {"宏观", "政策", "行业", "公司", "国际", "资金", "市场", "综合"}
CATEGORY_ALIAS = {
    "策略": "综合", "大宗商品": "宏观", "商品": "宏观",
    "债券": "宏观", "汇率": "国际", "科技": "行业", "消费": "行业",
    "金融": "宏观", "房地产": "行业", "能源": "行业", "医药": "行业",
    "其他": "综合", "快讯": "综合", "财经": "综合",
}
ALLOWED_MARKETS = {"A股", "美股", "港股", "全球", "无"}
MARKET_ALIAS = {
    "中国": "A股", "国内": "A股", "大陆": "A股", "美国": "美股",
    "香港": "港股", "国际": "全球",
}


def _normalize_category(cat) -> str:
    """归一化类别。**兜底为「综合」而非「行业」**——避免行业成为垃圾桶。"""
    cat = (cat or "").strip()
    if cat in ALLOWED_CATEGORIES:
        return cat
    return CATEGORY_ALIAS.get(cat, "综合")


def _normalize_market(mkt) -> str:
    mkt = (mkt or "").strip()
    if mkt in ALLOWED_MARKETS:
        return mkt
    return MARKET_ALIAS.get(mkt, "无")


def _extract_json(text: str):
    """从 LLM 输出中容错提取 JSON（优先整个解析，失败则抓 [..] 片段）。"""
    text = (text or "").strip()
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ================= 步骤①：LLM 相关性分类 =================
def classify_news(items: list[NewsItem]) -> list[dict]:
    """LLM 批量分类。返回 [{item, relevant, category, market, themes}]。"""
    if not items:
        return []
    llm = get_llm(temperature=0.0, part="news")
    results: list[dict] = []
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        news_list = "\n".join(
            f"{j + 1}. {it.title}｜{(it.content or '')[:100]}"
            for j, it in enumerate(batch)
        )
        prompt = CLASSIFY_PROMPT.format(news_list=news_list)

        def _classify(p: str) -> list:
            d = _extract_json(llm.invoke(p).content)
            if not isinstance(d, list):
                raise ValueError("LLM 分类输出非数组")
            return d

        try:
            data = call_with_retry(_classify, prompt, retry_label="新闻分类",
                                   retry_times=llm_retry_times())
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                idx = int(entry.get("id", -1)) - 1
                if 0 <= idx < len(batch):
                    themes = entry.get("themes") or []
                    if isinstance(themes, str):
                        themes = [themes]
                    results.append({
                        "item": batch[idx],
                        "relevant": bool(entry.get("relevant", False)),
                        "category": _normalize_category(entry.get("category")),
                        "market": _normalize_market(entry.get("market")),
                        "themes": [str(t) for t in themes],
                    })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLM 分类失败: {e}")
    return results


# ================= 步骤②：语义去重 =================
def _find_similar(db: Session, text: str) -> tuple[News, float] | None:
    """查库内最相似的一条新闻，返回 (news, similarity) 或 None。"""
    if vector_store.count() == 0:
        return None
    emb = embed_query(text)
    res = vector_store.get_collection().query(query_embeddings=[emb], n_results=1)
    ids = (res.get("ids") or [[]])[0]
    if not ids:
        return None
    dists = (res.get("distances") or [[]])[0]
    sim = 1.0 - float(dists[0])
    news = db.query(News).filter(News.id == ids[0]).first()
    if news is None:
        return None
    return news, sim


def _llm_is_duplicate(a: News, b_title: str, b_source: str) -> bool:
    """灰色区间复核：LLM 判断两条是否同一事件。"""
    llm = get_llm(temperature=0.0, part="news")
    prompt = DEDUP_PROMPT.format(
        source_a=a.source, title_a=a.title,
        source_b=b_source, title_b=b_title[:150],
    )
    try:
        resp = llm.invoke(prompt)
        return "true" in (resp.content or "").lower()
    except Exception:  # noqa: BLE001
        return False


# ================= 步骤③：多源合并 =================
def _merge_into(db: Session, existing: News, item: NewsItem) -> None:
    """把 item 合并进已有记录：累加 source_count / source_urls。"""
    urls: list[dict] = []
    if existing.source_urls:
        try:
            urls = json.loads(existing.source_urls)
        except json.JSONDecodeError:
            urls = []
    all_sources = {(existing.source, existing.url)} | {
        (u.get("source"), u.get("url")) for u in urls if isinstance(u, dict)
    }
    if item.url:
        all_sources.add((item.source, item.url))
    existing.source_urls = json.dumps(
        [{"source": s, "url": u} for s, u in all_sources if s],
        ensure_ascii=False,
    )
    existing.source_count = len(all_sources)
    # 信息更全（正文更长）则更新主内容
    if len(item.content or "") > len(existing.content or ""):
        existing.title = item.title
        existing.content = item.content


def _insert_new(db: Session, c: dict) -> News:
    """插入新新闻（带分类标签 + 初始多源信息）。"""
    item: NewsItem = c["item"]
    n = News(
        title=item.title,
        content=item.content or "",
        source=item.source,
        url=item.url or None,
        publish_time=item.publish_time or datetime.now(),
        category=c["category"],
        market=c["market"],
        themes=json.dumps(c["themes"], ensure_ascii=False),
        source_count=1,
        source_urls=json.dumps(
            [{"source": item.source, "url": item.url}] if item.url else [],
            ensure_ascii=False,
        ),
    )
    db.add(n)
    return n


# ================= 步骤④：索引 =================
def _index_new(news_list: list[News]) -> None:
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


def _rebuild_bm25(db: Session, days: int = 7) -> None:
    since = datetime.now() - timedelta(days=days)
    news_list = db.query(News).filter(News.publish_time >= since).all()
    chunks = [
        {"id": n.id, "text": f"{n.title}\n{n.content}", "metadata": {"news_id": n.id}}
        for n in news_list
    ]
    get_retriever().build_bm25_index(chunks)


# ================= 主流程 =================
def _dedup_by_url(items: list[NewsItem]) -> list[NewsItem]:
    """按 url 去重（翻页边界会产生重复 url），url 为空则不去重。"""
    seen: set[str] = set()
    result: list[NewsItem] = []
    for it in items:
        if it.url:
            if it.url in seen:
                continue
            seen.add(it.url)
        result.append(it)
    return result


def run_data_agent(db: Session) -> dict:
    """数据采集 Agent 主流程，返回统计 dict。"""
    # 1. 采集 + 按 url 去重（翻页会产生重复 url）
    raw = _dedup_by_url(collect_all_news())
    # 2. 相关性过滤
    classified = classify_news(raw)
    relevant = [c for c in classified if c["relevant"]]
    dropped = len(classified) - len(relevant)
    # 3. 去重 + 合并 + 入库
    new_news: list[News] = []
    merged = 0
    total_new = 0  # 累计新增（不能用 len(new_news)：分批后会被清空）
    for c in relevant:
        item: NewsItem = c["item"]
        text = f"{item.title} {item.content or ''}"
        sim = _find_similar(db, text)
        if sim and sim[1] >= DUP_THRESHOLD:
            _merge_into(db, sim[0], item)
            merged += 1
        elif sim and sim[1] >= GRAY_LOW:
            if _llm_is_duplicate(sim[0], text, item.source):
                _merge_into(db, sim[0], item)
                merged += 1
            else:
                new_news.append(_insert_new(db, c))
                total_new += 1
        else:
            new_news.append(_insert_new(db, c))
            total_new += 1
        # 分批 commit + 索引，让后续新闻能检出本批内的跨源重复
        if len(new_news) >= BATCH_INDEX_SIZE:
            db.commit()
            _index_new(new_news)
            new_news = []
    db.commit()
    # 4. 索引剩余新入库的 + 重建 BM25
    if new_news:
        _index_new(new_news)
    _rebuild_bm25(db)

    result = {
        "collected": len(raw),
        "classified": len(classified),
        "relevant": len(relevant),
        "dropped": dropped,
        "new": total_new,
        "merged": merged,
    }
    logger.info(
        f"数据采集Agent: 采集{result['collected']} 相关{result['relevant']} "
        f"丢弃{dropped} 新增{result['new']} 合并{merged}"
    )
    return result
