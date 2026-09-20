"""LLM 封装：DeepSeek（OpenAI 兼容协议），支持按环节分组配置。

分组的意义：让不同环节可以各接一个服务商 / 模型，互不影响。
每个分组的三项配置留空时，回落到 `DEEPSEEK_*`——所以**不配任何分组变量，
行为与拆分前完全一致**。
"""
from langchain_core.callbacks import BaseCallbackHandler
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

# 分组名 -> (api_key 字段, base_url 字段, model 字段, 关闭思考的开关字段)
_GROUPS = {
    "news": ("news_api_key", "news_base_url", "news_model",
             "news_disable_thinking"),
    "expert": ("expert_api_key", "expert_base_url", "expert_model",
               "expert_disable_thinking"),
    "chat": ("chat_api_key", "chat_base_url", "chat_model",
             "chat_disable_thinking"),
    "weekly": ("weekly_api_key", "weekly_base_url", "weekly_model",
               "weekly_disable_thinking"),
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


# 每个分组「实际命中」的模型名（备用生效时会被覆盖成备用模型名）。
# 由 _ModelRecorder 在每次调用开始时写入，供 get_llm_model_name 溯源。
# 主备并存时，最后一次写入的就是真正产出结果的那个模型。
_USED_MODEL: dict[str, str] = {}


class _ModelRecorder(BaseCallbackHandler):
    """记录该分组实际调用到的模型名。

    只在启用备用时挂上——没挂即说明主模型必被使用，
    `get_llm_model_name` 会照常回落到主模型名。
    """

    def __init__(self, part: str) -> None:
        self.part = part

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        name = (kwargs.get("metadata") or {}).get("ls_model_name")
        if name:
            _USED_MODEL[self.part] = name


def _group_fields(part: str) -> tuple[str, str, str, str]:
    """取分组字段名；分组名写错直接抛错，而不是静默回落到默认模型。"""
    if part not in _GROUPS:
        raise ValueError(f"未知的 LLM 分组：{part!r}，可选 {sorted(_GROUPS)}")
    return _GROUPS[part]


def resolve(part: str = DEFAULT_PART) -> tuple[str, str, str]:
    """解析某分组的生效配置，返回 (api_key, base_url, model)。

    分组名写错会直接抛错，而不是静默回落到默认模型——避免"配了不生效"。
    """
    key_field, url_field, model_field, _ = _group_fields(part)
    # 先 strip 再判空：填了空白字符串（" "）等于没填。不然空白会被当成有效值
    # 传给服务商，报错时很难看出根因
    return (
        (getattr(settings, key_field) or "").strip() or settings.deepseek_api_key.strip(),
        (getattr(settings, url_field) or "").strip() or settings.deepseek_base_url.strip(),
        (getattr(settings, model_field) or "").strip() or settings.deepseek_model.strip(),
    )


def _make_client(api_key: str, base_url: str, model: str,
                 temperature: float, part: str | None = None) -> ChatOpenAI:
    """构建客户端。`part` 只用于决定是否关闭思考模式，传 None 表示不关。

    显式设 `request_timeout`：不设时 openai SDK 默认 600s，
    主备都不可达时单个 attempt 会拖到 1200s。
    """
    kwargs = {}
    if part is not None and getattr(settings, _group_fields(part)[3]):
        # enable_thinking 是百炼 / qwen 系特有参数，只在开关打开时才发
        kwargs["extra_body"] = {"enable_thinking": False}
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        request_timeout=settings.llm_timeout,
        max_retries=0,  # 关掉 SDK 内层重试：重试统一由 app.retry 控制，避免嵌套放大
        **kwargs,
    )


def _build_fallback(temperature: float, part: str) -> ChatOpenAI | None:
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
        part,
    )


def fallback_enabled() -> bool:
    """备用模型是否已配齐。"""
    return bool(settings.llm_fallback_api_key
                and settings.llm_fallback_base_url
                and settings.llm_fallback_model)


def llm_retry_times() -> int:
    """LLM 调用点该用几次重试。

    启用备用时降为 1（不重试）：外层 `app.retry` 的重试与 fallback 切换会叠加，
    主备都失败时一次调用会打 **6 次**请求（3 轮 × 主备各 1 次）。
    降为 1 后总请求数回到 2 次（主 1 + 备 1），与未配备用时的 3 次相当。
    """
    return 1 if fallback_enabled() else 3


def get_llm(temperature: float = 0.0, part: str = DEFAULT_PART):
    """取某个分组的 LLM 客户端。

    **未配备用模型时返回 `ChatOpenAI`**（类型与行为同改动前）；
    配了备用才返回 `RunnableWithFallbacks`，主模型在
    `_FALLBACK_EXCEPTIONS` 命中的错误上失败时自动切备用再试一次。
    """
    api_key, base_url, model = resolve(part)
    primary = _make_client(api_key, base_url, model, temperature, part)

    fallback = _build_fallback(temperature, part)
    if fallback is None:
        return primary
    # 备用与主模型三项全同 → 没有意义，不必多打一次
    if (settings.llm_fallback_api_key == api_key
            and settings.llm_fallback_base_url == base_url
            and settings.llm_fallback_model == model):
        return primary
    return primary.with_fallbacks(
        [fallback], exceptions_to_handle=_FALLBACK_EXCEPTIONS
    ).with_config(callbacks=[_ModelRecorder(part)])


def get_llm_model_name(part: str = DEFAULT_PART) -> str:
    """该分组**实际产出**报告用的模型名。

    备用生效过就返回备用模型名（由 `_ModelRecorder` 记录），
    否则返回主模型名——未配备用时行为与改动前完全一致。
    """
    return _USED_MODEL.get(part) or resolve(part)[2]
