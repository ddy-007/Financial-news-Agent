"""LLM 封装：DeepSeek（OpenAI 兼容协议），支持按环节分组配置。

分组的意义：让不同环节可以各接一个服务商 / 模型，互不影响。
每个分组的三项配置留空时，回落到 `DEEPSEEK_*`——所以**不配任何分组变量，
行为与拆分前完全一致**。
"""
from langchain_openai import ChatOpenAI
from openai import (
    APIConnectionError,
    AuthenticationError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from app.config import settings

DEFAULT_PART = "expert"

# 分组名 -> (api_key 字段, base_url 字段, model 字段)
_GROUPS = {
    "news": ("news_api_key", "news_base_url", "news_model"),
    "expert": ("expert_api_key", "expert_base_url", "expert_model"),
    "chat": ("chat_api_key", "chat_base_url", "chat_model"),
    "weekly": ("weekly_api_key", "weekly_base_url", "weekly_model"),
}

# 触发切换到「备用模型」的异常类型。
# 判定口径与 app/retry.py 同一立场：**按类型/状态码判定，绝不匹配错误消息文本**。
# 只排除 400 BadRequestError —— 请求本身有问题，换谁都会失败，切了纯属白等。
_FALLBACK_EXCEPTIONS = (
    APIConnectionError,     # 连接失败 / 网络不可达（APITimeoutError 是其子类，一并覆盖）
    RateLimitError,         # 429 限流
    InternalServerError,    # 5xx 服务端错误
    NotFoundError,          # 404 模型不存在 / 无权访问
    AuthenticationError,    # 401 key 失效、被撤销或欠费
    PermissionDeniedError,  # 403 无权限
)


def resolve(part: str = DEFAULT_PART) -> tuple[str, str, str]:
    """解析某分组的生效配置，返回 (api_key, base_url, model)。

    分组名写错会直接抛错，而不是静默回落到默认模型——避免"配了不生效"。
    """
    if part not in _GROUPS:
        raise ValueError(f"未知的 LLM 分组：{part!r}，可选 {sorted(_GROUPS)}")
    key_field, url_field, model_field = _GROUPS[part]
    return (
        getattr(settings, key_field) or settings.deepseek_api_key,
        getattr(settings, url_field) or settings.deepseek_base_url,
        getattr(settings, model_field) or settings.deepseek_model,
    )


def _make_client(api_key: str, base_url: str, model: str,
                 temperature: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_retries=0,  # 关掉 SDK 内层重试：重试统一由 app.retry 控制，避免嵌套放大
    )


def _build_fallback(temperature: float) -> ChatOpenAI | None:
    """构建备用客户端。三项未配全 → None（不启用备用）。"""
    if not (settings.llm_fallback_api_key
            and settings.llm_fallback_base_url
            and settings.llm_fallback_model):
        return None
    return _make_client(
        settings.llm_fallback_api_key,
        settings.llm_fallback_base_url,
        settings.llm_fallback_model,
        temperature,
    )


def get_llm(temperature: float = 0.0, part: str = DEFAULT_PART):
    """取某个分组的 LLM 客户端。

    **未配备用模型时返回 `ChatOpenAI`**（类型与行为同改动前）；
    配了备用才返回 `RunnableWithFallbacks`，主模型在
    `_FALLBACK_EXCEPTIONS` 命中的错误上失败时自动切备用再试一次。
    """
    api_key, base_url, model = resolve(part)
    primary = _make_client(api_key, base_url, model, temperature)

    fallback = _build_fallback(temperature)
    if fallback is None:
        return primary
    # 备用与主模型三项全同 → 没有意义，不必多打一次
    if (settings.llm_fallback_api_key == api_key
            and settings.llm_fallback_base_url == base_url
            and settings.llm_fallback_model == model):
        return primary
    return primary.with_fallbacks([fallback],
                                  exceptions_to_handle=_FALLBACK_EXCEPTIONS)


def get_llm_model_name(part: str = DEFAULT_PART) -> str:
    return resolve(part)[2]
