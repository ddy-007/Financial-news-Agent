"""新闻业务：采集 + 去重入库 + 向量索引。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.collectors.news_collector import NewsItem
from app.models.news import News
from app.rag import vector_store
from app.rag.embeddings import embed_documents
from app.rag.retriever import get_retriever


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


def collect_and_store_news(db: Session) -> int:
    """采集 + 过滤 + 去重合并 + 索引（走数据采集 Agent），返回新增条数。"""
    from app.agent.data_agent import run_data_agent

    result = run_data_agent(db)
    return result["new"]
