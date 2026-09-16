"""ChromaDB 向量库封装（持久化）。"""
import chromadb

from app.config import settings

_client = None
_collection = None

COLLECTION_NAME = "financial_news"


def get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
    return _client


def get_collection():
    global _collection
    if _collection is None:
        _collection = get_client().get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
    return _collection


def _clean_metadata(meta: dict) -> dict:
    """ChromaDB 不接受 None 值，转成空字符串。"""
    return {k: ("" if v is None else v) for k, v in (meta or {}).items()}


def add_documents(ids: list[str], documents: list[str],
                  embeddings: list[list[float]], metadatas: list[dict]) -> None:
    get_collection().add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=[_clean_metadata(m) for m in metadatas],
    )


def upsert_documents(ids: list[str], documents: list[str],
                     embeddings: list[list[float]], metadatas: list[dict]) -> None:
    get_collection().upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=[_clean_metadata(m) for m in metadatas],
    )


def delete_documents(ids: list[str]) -> None:
    get_collection().delete(ids=ids)


def count() -> int:
    return get_collection().count()
