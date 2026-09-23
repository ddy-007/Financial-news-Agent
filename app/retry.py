"""统一的失败重试。

**设计立场**：与其把「降级」标记得更整齐，不如让降级不发生。
大部分降级（专家缺席、采集丢源、日历失效）的根因都是**偶发的网络/服务抖动**，
重试即可消除——本模块就是把这件事统一做掉。

重试 3 次的效果（单次失败率 5% → 0.0125%，降约 400 倍）。

**不重试**「重试也没用」的错误（认证失败、参数错误、配额问题），避免白等。
"""
from __future__ import annotations

import functools
import time
from typing import Callable

from loguru import logger

DEFAULT_TIMES = 3
DEFAULT_BASE_DELAY = 1.0

# 命中即**不重试**的异常类型名（小写）。按**类型**判定而非消息文本。
NO_RETRY_TYPES = (
    "authenticationerror", "permissiondeniederror", "badrequesterror",
    "notfounderror", "unprocessableentityerror", "apiresponsevalidationerror",
)

# 这些 4xx 属于「等一等可能就好」，仍应重试
RETRYABLE_4XX = (408, 409, 425, 429)


def _extract_status(exc: Exception) -> int | None:
    """从异常中提取 HTTP 状态码（httpx / requests / openai 各有不同位置）。"""
    v = getattr(exc, "status_code", None)
    if isinstance(v, int):
        return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None


def _should_retry(exc: Exception) -> bool:
    """判断异常是否值得重试。

    **按状态码与异常类型判定，绝不匹配错误消息文本**——
    消息里常含无关数字（如 eastmoney 的 `req_trace=1757888400000`），
    用子串匹配会把可重试的 500/超时误判为不可重试，反而废掉重试。
    """
    # 1. HTTP 状态码优先
    status = _extract_status(exc)
    if status is not None:
        if status in RETRYABLE_4XX:
            return True
        if 400 <= status < 500:
            return False          # 其余 4xx：客户端错误，重试无意义
        return True               # 5xx / 3xx：服务端或重定向问题，可重试

    # 2. 异常类型名（覆盖不带 status_code 的 SDK 异常）
    name = type(exc).__name__.lower()
    if any(k in name for k in NO_RETRY_TYPES):
        return False

    # 3. 默认重试（网络超时、连接重置、JSON 解析失败、LLM 输出不稳等）
    return True


def is_retryable(exc: Exception) -> bool:
    """公开版 `_should_retry` —— 供**调用方**在重试之外判断错误性质。

    **为什么需要**（B1，2026-09-24）：分类循环里每批之间要决定「退避多久再打下一批」。
    退避对 401/403/400 是纯白等（换谁都不会好），对限流/连接错误才是对的 ——
    于是那个循环需要同一个判定。**判据只保留一份**：这里转发，不复制逻辑，
    否则两处迟早会漂移（"同一份事实的两个副本"，本模块与契约脚本要防的正是这个）。

    与 `_should_retry` 的关系：那个是「装饰器内部要不要再试一次」，
    这个是「调用方要不要等一会儿」—— 同源，只是使用位置不同。
    """
    return _should_retry(exc)


def with_retry(fn: Callable | None = None, *, retry_times: int = DEFAULT_TIMES,
               retry_delay: float = DEFAULT_BASE_DELAY, retry_label: str = ""):
    """指数退避重试装饰器。

    用法：`@with_retry` 或 `@with_retry(retry_times=5, retry_label="新闻采集")`

    重试控制参数统一加 `retry_` 前缀，避免与被包装函数的同名形参冲突。
    """

    def deco(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            name = retry_label or getattr(func, "__name__", "call")
            last: Exception | None = None
            for i in range(max(1, retry_times)):
                try:
                    return func(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if not _should_retry(e):
                        logger.warning(f"[重试] {name}：不可重试的错误，直接抛出: {e}")
                        raise
                    if i == retry_times - 1:
                        break
                    delay = retry_delay * (2 ** i)
                    logger.warning(
                        f"[重试] {name} 第 {i + 1}/{retry_times} 次失败，{delay:.1f}s 后重试: {e}"
                    )
                    time.sleep(delay)
            logger.error(f"[重试] {name} 重试 {retry_times} 次后仍失败: {last}")
            raise last  # type: ignore[misc]
        return wrapper

    return deco(fn) if fn is not None else deco


def call_with_retry(fn: Callable, *args, retry_times: int = DEFAULT_TIMES,
                    retry_delay: float = DEFAULT_BASE_DELAY,
                    retry_label: str = "", **kwargs):
    """函数式用法：`call_with_retry(ak.stock_zh_index_daily, symbol=sym, retry_label="A股指数")`。

    注意：`retry_times` / `retry_delay` / `retry_label` 是本函数的控制参数，
    不会传给 `fn`；其余关键字参数原样透传。
    """

    @with_retry(retry_times=retry_times, retry_delay=retry_delay,
                retry_label=retry_label or getattr(fn, "__name__", "call"))
    def _run():
        return fn(*args, **kwargs)

    return _run()
