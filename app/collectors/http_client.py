"""共享的 `httpx.Client` 单例。

**为什么要共享**：`_get_json` 原来每次请求都 `with httpx.Client(...)` 新建一个 ——
一轮采集最多约 60 次，每次都重新握手、重建连接池。而 httpx 官方明确 `Client`
**是线程安全的**，且推荐复用（连接池 + keep-alive 才能生效）。

**生命周期**：进程级单例。由 `app/main.py` 的 lifespan 在关闭时调用 `close_client()`。
脚本（`scripts/`）用完也可以调，不调则进程退出时由操作系统回收 —— 不够干净，但无害。
"""
from __future__ import annotations

import httpx
from loguru import logger

# 与改动前 `news_collector._HEADERS` **逐字一致**（2026-09-22 从那里搬来）。
# 换 UA 可能被源站拒绝 —— 不要顺手"优化"这个字符串。
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    """懒加载共享 client（照 `app/rag/embeddings.py` 的单例写法）。"""
    global _client
    if _client is None:
        _client = httpx.Client(
            headers=HEADERS,
            timeout=15,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10,
                                max_keepalive_connections=5,
                                keepalive_expiry=30),
        )
        logger.debug("[http] 共享 client 已创建")
    return _client


def close_client() -> None:
    """关闭共享 client。**幂等**：重复调用、未创建时调用都安全。"""
    global _client
    if _client is not None:
        try:
            _client.close()
        finally:
            # 无论 close() 是否抛错，都要把引用清掉 —— 否则下次 get_client()
            # 会拿到一个已关闭的 client，请求全部失败且很难看出原因。
            _client = None
            logger.debug("[http] 共享 client 已关闭")
