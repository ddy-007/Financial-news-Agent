"""混合检索器：dense(ChromaDB) + sparse(BM25) → RRF 融合 → bge-reranker 精排。

流程：query → 两路召回(候选k条) → RRF 融合 → reranker 精排 → top_k
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from app.rag.embeddings import embed_query
from app.rag.reranker import rerank
from app.rag.vector_store import get_collection


@dataclass
class Hit:
    id: str
    text: str
    metadata: dict = field(default_factory=dict)
    score: float = 0.0


class HybridRetriever:
    """维护内存 BM25 索引 + ChromaDB dense 索引。"""

    def __init__(self) -> None:
        self._bm25 = None
        self._bm25_ids: list[str] = []
        self._bm25_texts: list[str] = []
        self._bm25_meta: list[dict] = []
        self._degrade_warned = False  # 首次退化时告警，避免刷屏

    # ---- 健康状态（供启动预热与评估层检查） ----
    def is_ready(self) -> bool:
        """BM25（关键词检索）分支是否可用。"""
        return self._bm25 is not None and len(self._bm25_ids) > 0

    @property
    def size(self) -> int:
        return len(self._bm25_ids)

    # ---- BM25 索引（sparse 召回） ----
    def build_bm25_index(self, chunks: list[dict]) -> None:
        """全量重建 BM25。chunks: [{id, text, metadata}]

        空输入时**安全清空**——`BM25Okapi([])` 会抛 ZeroDivisionError，
        且异常发生在 ids 已赋值之后，会把对象留在「旧索引 + 空 ids」的半损坏状态，
        导致后续检索越界崩溃。
        """
        import jieba
        from rank_bm25 import BM25Okapi

        if not chunks:
            self._bm25 = None
            self._bm25_ids = []
            self._bm25_texts = []
            self._bm25_meta = []
            self._degrade_warned = False  # 允许下次退化时再次告警
            logger.warning("BM25 索引重建：传入 0 条，已清空（检索将退化为纯向量）")
            return

        # 全部在局部变量里构造，**成功后一次性赋值给 self**——
        # 否则中途抛错会留下「旧 _bm25 + 新 ids」的半损坏状态，检索时越界
        ids = [c["id"] for c in chunks]
        texts = [c["text"] for c in chunks]
        meta = [c.get("metadata", {}) for c in chunks]
        tokenized = [list(jieba.cut(t)) for t in texts]
        bm25 = BM25Okapi(tokenized)

        self._bm25 = bm25
        self._bm25_ids = ids
        self._bm25_texts = texts
        self._bm25_meta = meta
        self._degrade_warned = False
        logger.info(f"BM25 索引已重建，共 {len(chunks)} 条")

    # ---- dense 召回 ----
    def _dense_search(self, query: str, top_k: int, *, ids: list[str] | None = None) -> list[Hit]:
        if ids == []:
            return []
        collection = get_collection()
        if ids is not None:
            # Chroma.query(ids=...) 遇到库内不存在的任一 ID 会报错。
            # SQL 可见但尚未索引的记录仍可走本次窗口的 BM25。
            ids = collection.get(ids=ids, include=[]).get("ids") or []
            if not ids:
                return []
        emb = embed_query(query)
        kwargs = {"ids": ids} if ids is not None else {}
        res = collection.query(query_embeddings=[emb],
                               n_results=min(top_k, len(ids)) if ids is not None else top_k,
                               **kwargs)
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        hits = []
        for i, cid in enumerate(ids):
            hits.append(Hit(
                id=cid,
                text=docs[i] if i < len(docs) else "",
                metadata=metas[i] if i < len(metas) else {},
                score=1.0 - float(dists[i]) if i < len(dists) else 0.0,
            ))
        return hits

    # ---- sparse 召回 ----
    def _sparse_search(self, query: str, top_k: int) -> list[Hit]:
        # 用 is_ready() 统一判定：只看 _bm25 不够——空重建后 _bm25 会残留旧对象
        if not self.is_ready():
            # 不再完全静默：首次退化时告警一次，说明检索质量已下降
            if not self._degrade_warned:
                logger.warning(
                    "BM25 索引不可用，本次及后续检索将退化为**纯向量检索**"
                    "（关键词精确匹配能力缺失）。若持续如此，请检查启动预热与新闻入库。"
                )
                self._degrade_warned = True
            return []
        import jieba
        tokens = list(jieba.cut(query))
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_k]
        return [
            Hit(
                id=self._bm25_ids[i],
                text=self._bm25_texts[i],
                metadata=self._bm25_meta[i],
                score=float(scores[i]),
            )
            for i in ranked
        ]

    # ---- RRF 融合 ----
    @staticmethod
    def _rrf(hits_list: list[list[Hit]], k: int = 60) -> list[Hit]:
        score: dict[str, float] = {}
        doc_map: dict[str, Hit] = {}
        for hits in hits_list:
            for rank, h in enumerate(hits):
                score[h.id] = score.get(h.id, 0.0) + 1.0 / (k + rank + 1)
                doc_map.setdefault(h.id, h)
        ranked = sorted(score.items(), key=lambda x: -x[1])
        return [doc_map[cid] for cid, _ in ranked]

    # ---- 对外主入口 ----
    def hybrid_search(self, query: str, top_k: int = 5, candidate_k: int = 20, *,
                      since: datetime | None = None, until: datetime | None = None) -> list[Hit]:
        if top_k < 1 or candidate_k < 1:
            return []
        if since is None and until is None:
            # 保留底层无窗口调用的旧行为；问答工具会明确传入时间边界。
            dense = self._dense_search(query, candidate_k)
            sparse = self._sparse_search(query, candidate_k)
        else:
            from app.db import SessionLocal
            from app.models.news import News

            cutoff = until or datetime.now()
            if cutoff.tzinfo is not None or (since is not None and since.tzinfo is not None):
                raise ValueError("检索时间请使用本地无时区 datetime")
            if since is not None and since > cutoff:
                raise ValueError("检索起点不得晚于截止时间")
            with SessionLocal() as db:
                q = db.query(News).filter(News.publish_time <= cutoff)
                if since is not None:
                    q = q.filter(News.publish_time >= since)
                chunks = [{"id": row.id, "text": f"{row.title}\n{row.content or ''}", "metadata": {
                    "news_id": row.id, "title": row.title, "source": row.source,
                    "publish_time": row.publish_time.isoformat(), "category": row.category or "",
                }} for row in q.order_by(News.id).all()]
            if not chunks:
                return []
            # 按主键限制 Chroma，无需迁移其旧字符串时间元数据，且在 top-k 之前过滤。
            allowed = {chunk["id"]: chunk for chunk in chunks}
            try:
                dense = self._dense_search(query, candidate_k, ids=list(allowed))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[检索] 窗口内向量召回失败，使用同窗关键词召回：{type(exc).__name__}")
                dense = []
            local = HybridRetriever()
            local.build_bm25_index(chunks)
            sparse = local._sparse_search(query, candidate_k)
            # 两路都以数据库正文/日期为准，过期索引或替身也不能把窗口外 ID 带回来。
            dense = [Hit(id=h.id, text=allowed[h.id]["text"], metadata=allowed[h.id]["metadata"],
                         score=h.score) for h in dense if h.id in allowed]
        fused = self._rrf([dense, sparse])[:candidate_k]
        if not fused:
            return []
        texts = [h.text for h in fused]
        scores = rerank(query, texts)
        for h, s in zip(fused, scores):
            h.score = float(s)
        fused.sort(key=lambda h: -h.score)
        return fused[:top_k]


_retriever: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever
