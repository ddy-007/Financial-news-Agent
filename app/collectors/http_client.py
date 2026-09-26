"""共享的 `httpx.Client` 单例。

**为什么要共享**：`_get_json` 原来每次请求都 `with httpx.Client(...)` 新建一个 ——
一轮采集最多约 60 次，每次都重新握手、重建连接池。而 httpx 官方明确 `Client`
**是线程安全的**，且推荐复用（连接池 + keep-alive 才能生效）。

**生命周期**：进程级单例。由 `app/main.py` 的 lifespan 在关闭时调用 `close_client()`。
脚本（`scripts/`）用完也可以调，不调则进程退出时由操作系统回收 —— 不够干净，但无害。

⚠️ **连接类故障后必须能重建**（2026-09-26 实机教训）：`Client` 把连接池、
SSL 上下文、代理挂载在**创建时**就固定下来。实测后端连续运行数小时后，三个新闻源
**一起**变成 TLS 握手超时，而同一台机器上**新起的进程完全正常** —— 重启后端立刻恢复。
也就是说坏的是**进程内这个对象**，不是外网。`reset_client()` 与
`is_connection_error()` 就是为此存在：让采集在连接类故障后自愈，不必人工重启。
"""
from __future__ import annotations

import os
import ssl

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


def _mask_proxy(value: str) -> str:
    """代理地址里的 `user:pass@` 必须掩码 —— 日志会被存档、导出。"""
    if "@" in value:
        scheme, sep, rest = value.partition("://")
        _, _, host = rest.rpartition("@")
        return f"{scheme}{sep}***@{host}" if sep else "***"
    return value


def _proxy_env() -> dict[str, str]:
    """当前生效的代理环境变量（已掩码）。

    httpx 在**创建 client 时**读一次这些变量就固定下来，之后再改环境对已有
    client 无效。所以这条记录是排查「代理配置滞留」的第一手证据。
    """
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    return {n: _mask_proxy(os.environ[n]) for n in names if os.environ.get(n)}


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
        logger.debug(
            f"[http] 共享 client 已创建（代理环境变量：{_proxy_env() or '无'}）"
        )
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


def reset_client() -> None:
    """丢弃当前共享 client，下次 `get_client()` 重建一个。**幂等**。

    与 `close_client()` 是**同一件事的两个名字** —— 分开命名只是让调用点的
    **意图**可读：`close_client()` = 「进程要退出了」，`reset_client()` =
    「这个对象坏了，换一个」。行为必须一致，**不要在这里另写一套**。
    """
    close_client()


def is_connection_error(exc: BaseException) -> bool:
    """是不是「这条连接坏了」—— 换一条新连接才可能好的那一类错误。

    `httpx.TransportError` 家族覆盖了网络层的主要失败：`ConnectError`（连不上 /
    DNS 失败 / 连接被拒）、`ConnectTimeout`（建连或 TLS 握手超时）、`ReadTimeout`
    （连上了但不回数据）、`RemoteProtocolError`（对端把连接掐了）、`ProxyError`。
    裸的 `ssl.SSLError` / `OSError` 也一并收下 —— SDK 偶尔会把底层错误原样抛出。

    **判据是异常类型，不看消息文本**：httpx 重新包装 httpcore 异常时会丢消息
    （实测 `ConnectTimeout('')`，`str(e)` 是空串），靠文本匹配必然误判。

    反过来，HTTP 400/404/500 这类**服务端明确回应**的错误**不算**连接故障 ——
    换个连接打过去还是同样的结果，重建 client 纯属浪费。
    """
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, (ssl.SSLError, OSError))
