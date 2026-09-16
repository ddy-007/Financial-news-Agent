"""bge-reranker-v2-m3 重排模型封装（cross-encoder，进程内单例）。"""
from loguru import logger

from app.config import settings

_reranker = None


def get_reranker():
    global _reranker
    if _reranker is None:
        from FlagEmbedding import FlagReranker

        logger.info(f"正在加载 reranker: {settings.bge_reranker_path}")
        _reranker = FlagReranker(settings.bge_reranker_path, use_fp16=True)
        logger.info("reranker 加载完成")
    return _reranker


def rerank(query: str, documents: list[str]) -> list[float]:
    """对候选文档打分，返回分数列表（越大越相关），用于排序。"""
    if not documents:
        return []
    rk = get_reranker()
    pairs = [[query, doc] for doc in documents]
    scores = rk.compute_score(pairs)
    if not isinstance(scores, list):
        scores = [scores]
    return list(scores)
