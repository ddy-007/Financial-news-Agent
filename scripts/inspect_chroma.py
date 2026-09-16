"""查看 ChromaDB 向量库里存储了哪些新闻。

用法：.venv/Scripts/python.exe scripts/inspect_chroma.py
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.vector_store import get_collection  # noqa: E402


def main():
    coll = get_collection()
    total = coll.count()
    print(f"ChromaDB 总条数: {total}\n")

    data = coll.get(include=["metadatas", "documents"])
    metas = data.get("metadatas") or []
    ids = data.get("ids") or []

    # 按来源统计
    sources = Counter(m.get("source", "?") for m in metas)
    print("按来源统计:", dict(sources))

    # 按日期统计
    days = Counter((m.get("publish_time", "") or "")[:10] for m in metas)
    print("\n按日期统计:")
    for d in sorted(days.keys(), reverse=True)[:10]:
        print(f"  {d}: {days[d]} 条")

    # 列出前 N 条
    print("\n前 10 条:")
    for i in range(min(10, len(ids))):
        m = metas[i]
        print(
            f"  [{m.get('source', '')}] {m.get('title', '')[:50]} "
            f"({(m.get('publish_time', '') or '')[:10]})"
        )


if __name__ == "__main__":
    main()
