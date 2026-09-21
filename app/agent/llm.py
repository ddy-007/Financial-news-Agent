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


def resolve(part: str) -> tuple[str, str, str]:
    """解析某分组的生效配置，返回 (api_key, base_url, model)。

    `part` **必填**，理由同 `get_llm`：有默认值就会让漏填静默用错模型。
    分组名写错会直接抛错，而不是静默回落到默认模型——避免"配了不生效"。

    ⚠️ **返回值含明文 api_key，不要整个打印**（会泄露到对话/日志）。
    要打印配置请只取下标，如 `resolve(p)[2]` 取模型名。
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


def fallback_models() -> list[str]:
    """按顺序返回**去重后**的备用模型名。

    顺序 = `LLM_FALLBACK_MODEL` 在前，`LLM_FALLBACK_MODELS` 里逗号分隔的依次跟上 ——
    也就是说前一个失败才会轮到后一个。

    去重是必要的：重复的模型名会让同一个请求白打一遍（换谁都失败）。
    空串与纯空白项一律丢弃。
    """
    raw = [settings.llm_fallback_model]
    raw += settings.llm_fallback_models.split(",")
    out: list[str] = []
    for m in raw:
        m = (m or "").strip()
        if m and m not in out:
            out.append(m)
    return out


def effective_fallback_models(part: str) -> list[str]:
    """`part` 这个分组**实际**会挂上的备用模型名。

    与 `fallback_models()` 的区别：这里**剔除了与主模型三项（key/base_url/model）全同的项**
    —— 换谁都一样，多打一次没有意义。

    **这个区别一定要保留**：2026-09-21 就出现过「主模型与备用同名、key/url 也相同」
    的配置 —— 配置里看着有备用，实际一个都挂不上。谁按"配置非空"去判断
    「有没有备用」，谁就会在那种配置下做出错误决定（见 `llm_retry_times`）。
    """
    if not (settings.llm_fallback_api_key and settings.llm_fallback_base_url):
        return []
    key, base_url, model = resolve(part)
    return [
        name for name in fallback_models()
        if not (settings.llm_fallback_api_key == key
                and settings.llm_fallback_base_url == base_url
                and name == model)
    ]


def _build_fallbacks(temperature: float, part: str) -> list[ChatOpenAI]:
    """构建该分组的备用客户端列表。

    **名字来自 `effective_fallback_models(part)`，剔除规则只写那一处** ——
    绝不能在这里再复制一遍。一旦两处规则漂移，"判断有没有备用"与
    "实际挂几个"就会重新不一致，而本次改动的全部意义就是消灭这个不一致。
    """
    return [
        _make_client(settings.llm_fallback_api_key,
                     settings.llm_fallback_base_url,
                     name, temperature, part)
        for name in effective_fallback_models(part)
    ]


def fallback_enabled(part: str) -> bool:
    """`part` 这个分组**是否真的**挂上了备用。

    判据是「剔除之后还剩不剩」，不是「配置里填没填」。
    `part` 必填（同 `get_llm` 的理由）：备用是逐分组挂的，问"全局有没有备用"没有意义。
    """
    return bool(effective_fallback_models(part))


def llm_retry_times(*, part: str) -> int:
    """`part` 这个分组的 LLM 调用点该用几次重试。

    **按分组算，不按全局开关算。** 备用是逐分组挂的，"配了备用"不等于
    "这个分组真的挂上了"（与主模型三项全同的会被剔除）。

    若用全局开关，那种分组会落入最差组合：**实际没有备用，重试却被降成 1 次**
    —— 既没有兜底又少了重试，比不配备用还差。2026-09-21 在真实配置里发生过。

    降为 1 的理由：外层 `app.retry` 的重试与 fallback 切换会叠加。设 N = 该分组
    实际挂上的备用个数：

        降为 1 时，一次调用最坏打 **N+1** 次请求（主 1 + 备用 N，各试一次）
        不降（3）时，最坏会变成 3×(N+1) 次 —— 所以才要降

    **N 不要配太多**：每个失败的备用都要先等满 `llm_timeout`（默认 120 秒），
    日报有约 20 次 LLM 调用，N 大了最坏耗时会成倍放大。
    """
    return 1 if fallback_enabled(part) else 3


def get_llm(temperature: float = 0.0, *, part: str):
    """取某个分组的 LLM 客户端。

    `part` **必填**——故意不给默认值。给了默认值（如 "expert"）的话，
    将来新增调用点漏写 `part=` 不会报错，而是**静默用错模型**
    （比如新闻采集悄悄走了研判的模型），只有看账单才发现。
    现在漏写当场 `TypeError`，在写代码时就暴露。

    **未配备用模型时返回 `ChatOpenAI`**（类型与行为同改动前）；
    配了备用才返回 `RunnableWithFallbacks`，主模型在
    `_FALLBACK_EXCEPTIONS` 命中的错误上失败时，**按配置顺序逐个尝试备用**
    （顺序由 `effective_fallback_models` 定义，剔除规则也只写在那里），直到有一个成功。

    ⚠️ **已知局限**：`with_fallbacks` 的异常捕获对**每一个** runnable 都生效，
    所以若**中间某个备用**抛的是**未列入 `_FALLBACK_EXCEPTIONS`** 的异常
    （主要是 400），整条链会**当场中断**，排在它后面的备用**拿不到机会**。
    2026-09-21 实测确认。要让链路更耐断，得把 400 也纳入切换集（但那会让主模型的
    400 也去白打每一个备用），或自行实现逐级尝试。
    """
    api_key, base_url, model = resolve(part)
    primary = _make_client(api_key, base_url, model, temperature, part)

    # 备用的顺序即尝试顺序（见 effective_fallback_models）；
    # 与主模型三项全同的备用已在里面剔除，全被剔除就退回"不启用"
    fallbacks = _build_fallbacks(temperature, part)
    if not fallbacks:
        return primary
    return primary.with_fallbacks(
        fallbacks, exceptions_to_handle=_FALLBACK_EXCEPTIONS
    ).with_config(callbacks=[_ModelRecorder(part)])


def get_llm_model_name(*, part: str) -> str:
    """该分组**实际产出**报告用的模型名。

    `part` 必填，理由同 `get_llm`。

    备用生效过就返回备用模型名（由 `_ModelRecorder` 记录），
    否则返回主模型名——未配备用时行为与改动前完全一致。
    """
    return _USED_MODEL.get(part) or resolve(part)[2]
