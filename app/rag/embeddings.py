"""bge-m3 嵌入模型封装（进程内单例，只加载一次）。"""
from loguru import logger

from app.config import settings

_model = None


def get_embedding_model():
    """懒加载 bge-m3。"""
    global _model
    if _model is None:
        from FlagEmbedding import BGEM3FlagModel

        logger.info(f"正在加载 bge-m3: {settings.bge_m3_model_path}")
        _model = BGEM3FlagModel(settings.bge_m3_model_path, use_fp16=True)
        logger.info("bge-m3 加载完成")
    return _model


def embed_documents(texts: list[str]) -> list[list[float]]:
    """批量嵌入文档，返回 dense 向量列表（1024 维）。"""
    if not texts:
        return []
    model = get_embedding_model()
    out = model.encode(
        texts,
        batch_size=16,
        max_length=8192,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    return out["dense_vecs"].tolist()


def embed_query(text: str) -> list[float]:
    """嵌入单个查询。"""
    return embed_documents([text])[0]
