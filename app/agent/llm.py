"""LLM 封装：DeepSeek（OpenAI 兼容协议），支持按环节分组配置。

分组的意义：让不同环节可以各接一个服务商 / 模型，互不影响。
每个分组的三项配置留空时，回落到 `DEEPSEEK_*`——所以**不配任何分组变量，
行为与拆分前完全一致**。
"""
import json
from datetime import datetime, timedelta
from typing import Callable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable, RunnableBinding, RunnableLambda, RunnableWithFallbacks
from langchain_openai import ChatOpenAI
from loguru import logger
from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from app.config import settings
from app.agent.llm_errors import error_details

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


class InsufficientBalanceError(Exception):
    """402 Payment Required（余额不足 / 欠费）：**该切备用**的那一类。

    **为什么不能直接把 APIStatusError 塞进 _FALLBACK_EXCEPTIONS**（2026-10-21）：
    BadRequestError(400) 是 APIStatusError 的**子类**（实测
    issubclass(BadRequestError, APIStatusError) is True）。把父类放进切换集，
    等于把 400 也放进去 —— 而 400 是「请求本身有问题，换谁都会失败」，
    正是本清单一开始就刻意排除的那一类，切了纯属白等。

    402 则完全相反：它是**账号余额**的问题，与请求内容无关，
    换一个服务商的 key 大概率立刻就好。实测 2026-10-20 那轮：
    DeepSeek 余额耗尽 → 四位分析师 + 风险官 + 首席全部 402 落空 →
    备用明明配好且可用，却因为 402 不在白名单里而一次都没被调用 →
    最终落库一份「日报生成暂不可用」的占位行。

    所以这里**只把 402 这一档单独译出来**，而不是放宽到整个 APIStatusError。
    """

    def __init__(self, message: str, *, status_code: int = 402) -> None:
        super().__init__(message)
        self.status_code = status_code


class _FallbackAwareChatOpenAI(ChatOpenAI):
    """把「402 余额不足」译成 InsufficientBalanceError，让主备切换能接住。

    只改**可切换判定**，不改请求内容、不改重试策略、不改其余异常的行为：
    非 402 的 APIStatusError（含 400/422）仍按原样抛出 —— 它们不该切备用。
    """

    def _translate(self, exc: APIStatusError) -> None:
        """只在 `status_code == 402` 时改写异常；其余原样抛出。"""
        if getattr(exc, "status_code", None) == 402:
            raise InsufficientBalanceError(str(exc)) from exc

    # ⚠️ **四个入口都要覆写**：`invoke`/`ainvoke` 走 `_generate`/`_agenerate`，
    # 而 `stream`/`astream` 走 `_stream`/`_astream` —— `_generate_with_cache`
    # 在 `_should_stream()` 为真时**压根不调 `_generate`**。
    #
    # 2026-10-22 实测（httpx.MockTransport 构造 402，未联网）：
    #   · 只覆写 `_generate`/`_agenerate` 时，`stream()` 抛出**裸** APIStatusError，
    #     外层 `with_fallbacks` 连一次备用都不会试（请求序列只有主模型）。
    #   · 补上 `_stream`/`_astream` 后流式路径同样会被翻译。
    #
    # 当前全仓没有流式调用点（全是 `invoke`），所以这是**潜伏缺陷**；
    # 但"以后某个前端要逐字输出"是完全可能的需求，补这 12 行比留个坑便宜。
    #
    # 签名一律用 `*args, **kwargs` 透传：父类的 `_stream` 带**仅关键字**参数
    # `stream_usage`，逐字抄签名会在 langchain 升级加参数时先炸。
    def _generate(self, *args, **kwargs):
        try:
            return super()._generate(*args, **kwargs)
        except APIStatusError as exc:
            self._translate(exc)
            raise

    async def _agenerate(self, *args, **kwargs):
        try:
            return await super()._agenerate(*args, **kwargs)
        except APIStatusError as exc:
            self._translate(exc)
            raise

    def _stream(self, *args, **kwargs):
        try:
            # 生成器是**惰性**的：异常在迭代时才抛，所以 try 必须包住 `yield from`，
            # 不能只包住 `super()._stream(...)` 的构造。
            yield from super()._stream(*args, **kwargs)
        except APIStatusError as exc:
            self._translate(exc)
            raise

    async def _astream(self, *args, **kwargs):
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        except APIStatusError as exc:
            self._translate(exc)
            raise


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
    # **输出不合规**（JSON 解析失败 / schema 不符）—— 2026-09-22 补。
    #
    # 它不属于上面那类"服务不可用"，但同样该切备用：**换个模型往往就合规了**
    # （不同模型对输出格式的遵循度不同），比"用同一个模型再试一次"更有希望。
    #
    # ⚠️ 补它的**真正理由**是修一个被破坏的不变量：`llm_retry_times()` 把重试
    # 降为 1 的前提是「失败时备用会顶上一次机会」。而解析失败原先不在本清单里
    # → 备用不触发 + 重试又被降成 1 → **合计只有 1 次机会**，
    # 比"没配备用时的 3 次"还少。启用备用反而降低了这类失败的容错。
    # 实测（2026-09-22）：一轮 110 批里有一批正是这样丢掉 20 条的。
    #
    # 用宽泛的 `ValueError` 而非自定义异常类，是**故意**的：
    # 自定义类需要每个调用点记得用它，忘了就又回到这个问题；
    # 而宽一点的代价只是"非解析类 ValueError 也会多切一次备用"——
    # 那既不会静默、也不会误判，最多多打一次请求。
    ValueError,
    # **402 余额不足** —— 2026-10-21 补。
    #
    # 它不属于上面任何一个内置类型：402 抛的是**裸的** APIStatusError，
    # 而 AuthenticationError(401) / PermissionDeniedError(403) 是它的**兄弟**、
    # 不是父类（三者都直接继承 APIError）。于是 2026-10-20 那轮出现了最糟的组合：
    # 备用配好且可用，却**一次都没被调用**，五位专家 + 首席全部落空。
    #
    # 这里放的是**自定义类**而不是 APIStatusError：后者会把 400 也带进来，
    # 详见 InsufficientBalanceError 的 docstring。
    InsufficientBalanceError,
)


# 主模型「冷却到」的时刻（按分组）。非空且未到期 → `get_llm` 直接把主模型
# 从链首摘掉。见 `settings.llm_primary_cooldown_minutes` 的注释。
_PRIMARY_COOLDOWN: dict[str, datetime] = {}


def _primary_cooling(part: str) -> datetime | None:
    """主模型是否在冷却期；在则返回解冻时刻，否则 None（顺手清掉过期项）。"""
    until = _PRIMARY_COOLDOWN.get(part)
    if until is None:
        return None
    if until <= datetime.now():
        # 到期 = 半开：让下一次 `get_llm` 重新把主模型放进链首试一次
        del _PRIMARY_COOLDOWN[part]
        return None
    return until


def note_primary_failed(part: str) -> datetime | None:
    """主模型失败 → 开启冷却。

    **只在「新开一次冷却」时返回解冻时刻**，调用方据此决定要不要打日志 ——
    否则每一批都会重复打一遍（实测那 8 条重复 WARNING 就是这么来的）。
    已在冷却期内则返回 None（**不续期**：冷却窗口从第一次失败起固定）。
    """
    if _primary_cooling(part) is not None:
        return None
    if settings.llm_primary_cooldown_minutes <= 0:
        return None          # 配 0 = 关闭该行为
    until = datetime.now() + timedelta(
        minutes=settings.llm_primary_cooldown_minutes)
    _PRIMARY_COOLDOWN[part] = until
    return until


# 每个分组「实际命中」的模型名（备用生效时会被覆盖成备用模型名）。
# 由 _ModelRecorder 在每次调用开始时写入，供 get_llm_model_name 溯源。
# 主备并存时，最后一次写入的就是真正产出结果的那个模型。
_USED_MODEL: dict[str, str] = {}


class _ModelRecorder(BaseCallbackHandler):
    """记录该分组实际调用到的模型名，并在**启用备用时打日志**。

    只在启用备用时挂上——没挂即说明主模型必被使用，
    `get_llm_model_name` 会照常回落到主模型名。

    **为什么要打日志**：LangChain 的 `with_fallbacks` **默认静默切换**。
    更麻烦的是全部失败时它抛的是 `first_error`（主模型的错），
    所以日志里只会看到主模型的报错 —— **看起来像"备用压根没生效"**。
    2026-09-21 就因此误判过一次：实际是切了，但备用自己模型名写错（404）。
    """
    def __init__(self, part: str, primary_model: str) -> None:
        self.part = part
        self.primary_model = primary_model

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        name = (kwargs.get("metadata") or {}).get("ls_model_name")
        if not name:
            return
        _USED_MODEL[self.part] = name
        # 收到「非主模型」的 start 事件 = 主模型已经失败并切走了。
        # 注意本回调在**发起时**触发（不保证成功），所以措辞是"尝试切到"。
        if name != self.primary_model:
            # ⚠️ 只在**新开冷却**时打日志。冷却期内主模型根本不在链上，
            # 每批都会走到这里 —— 不加这个判断就是每批一条重复 WARNING
            # （实测 2026-09-23 那轮第 27–34 批打了 8 条）。
            until = note_primary_failed(self.part)
            if until is None:
                return
            logger.warning(
                f"[{self.part}] 主模型 {self.primary_model} 未成功，"
                f"尝试切到备用 {name}；**进入冷却**至 {until:%H:%M:%S}"
                f"（{settings.llm_primary_cooldown_minutes:.0f} 分钟内不再尝试主模型，"
                f"避免每批白打一次）"
            )


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
    # 用 _FallbackAwareChatOpenAI 而不是 ChatOpenAI：它只多一件事 ——
    # 把 402 余额不足译成 InsufficientBalanceError，好让主备切换接住。
    # 其余行为（模型名、超时、重试、extra_body）完全一致。
    return _FallbackAwareChatOpenAI(
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


def with_output_parser(llm, parser: Callable):
    """把注入客户端的解析步骤推入每级备用，保留绑定配置、回调和异常策略。"""
    if isinstance(llm, RunnableBinding):
        return llm.model_copy(update={"bound": with_output_parser(llm.bound, parser)})
    if isinstance(llm, RunnableWithFallbacks):
        return llm.model_copy(update={
            "runnable": with_output_parser(llm.runnable, parser),
            "fallbacks": [with_output_parser(fallback, parser) for fallback in llm.fallbacks],
        })
    # 简单 invoke 替身也能保持专家函数原有的客户端注入契约。
    runnable = llm if isinstance(llm, Runnable) else RunnableLambda(llm.invoke)
    return runnable | RunnableLambda(parser)


def with_structured_output(llm, schema):
    """每级主备独立校验；auto 工具选择兼容思考模式，无有效工具结果仍失败。"""
    if isinstance(llm, RunnableBinding):
        return llm.model_copy(update={"bound": with_structured_output(llm.bound, schema)})
    if isinstance(llm, RunnableWithFallbacks):
        return llm.model_copy(update={
            "runnable": with_structured_output(llm.runnable, schema),
            "fallbacks": [with_structured_output(fallback, schema) for fallback in llm.fallbacks],
        })
    # 思考模式不支持强制指定工具（实测 deepseek-flash 返回 HTTP 400）。
    # auto 保留原生工具 schema；是否调用及结果仍须通过下方严格校验。
    structured = llm.with_structured_output(schema, method="function_calling", tool_choice="auto")

    def invoke(messages, config, **kwargs):
        try:
            return schema.model_validate(structured.invoke(messages, config=config, **kwargs))
        except Exception as exc:
            logger.warning("[Critic] 结构化调用失败 model={} error={}",
                           getattr(llm, "model_name", "unknown"),
                           json.dumps(error_details(exc), ensure_ascii=False))
            raise

    return RunnableLambda(invoke)


def get_llm(temperature: float = 0.0, *, part: str,
            output_parser: Callable | None = None):
    """取某个分组的 LLM 客户端。

    `part` **必填**——故意不给默认值。给了默认值（如 "expert"）的话，
    将来新增调用点漏写 `part=` 不会报错，而是**静默用错模型**
    （比如新闻采集悄悄走了研判的模型），只有看账单才发现。
    现在漏写当场 `TypeError`，在写代码时就暴露。

    `output_parser` 非空时，每个模型分别执行「调用 + 解析校验」，
    解析异常也在备用切换边界内；返回值为解析结果。其他调用方保持消息返回值。

    未传解析器且未配备用模型时返回 `ChatOpenAI`（类型与行为同改动前）；
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

    def parsed(client):
        return client | RunnableLambda(output_parser) if output_parser else client

    # 备用的顺序即尝试顺序（见 effective_fallback_models）；
    # 与主模型三项全同的备用已在里面剔除，全被剔除就退回"不启用"
    fallbacks = [parsed(client) for client in _build_fallbacks(temperature, part)]
    if not fallbacks:
        return parsed(_make_client(api_key, base_url, model, temperature, part))

    cooling = _primary_cooling(part)
    if cooling is not None:
        # 主模型在冷却期 → **从链首摘掉**，用第一个备用当主。
        # 这样整轮都不再白打主模型（实测 2026-09-23 那轮白打了 8 次）。
        head, rest = fallbacks[0], fallbacks[1:]
        # 这里**必须是 debug**：`get_llm` 每批都调一次，冷却期内每批都会走到这一行。
        # 实测 2026-09-24 那轮：**6 秒 15 条**同样的 WARNING，把别的信息全淹了 ——
        # 这与 B1 ③ 要修的「日志爆炸」是同一个毛病，只是换了个位置。
        # 「进入冷却」那一条已经由 `_ModelRecorder` 在**新开冷却时**打过（见 :130），
        # 每批重复说一次不再增加任何信息。
        logger.debug(
            f"[{part}] 主模型 {model} 仍在冷却期（至 {cooling:%H:%M:%S}）—— "
            f"本轮**跳过主模型**，直接用 {effective_fallback_models(part)[0]}"
        )
        if not rest:
            return head.with_config(callbacks=[_ModelRecorder(part, model)])
        return head.with_fallbacks(
            rest, exceptions_to_handle=_FALLBACK_EXCEPTIONS
        ).with_config(callbacks=[_ModelRecorder(part, model)])

    primary = parsed(_make_client(api_key, base_url, model, temperature, part))
    return primary.with_fallbacks(
        fallbacks, exceptions_to_handle=_FALLBACK_EXCEPTIONS
    ).with_config(callbacks=[_ModelRecorder(part, model)])


def get_llm_model_name(*, part: str) -> str:
    """该分组**实际产出**报告用的模型名。

    `part` 必填，理由同 `get_llm`。

    备用生效过就返回备用模型名（由 `_ModelRecorder` 记录），
    否则返回主模型名——未配备用时行为与改动前完全一致。
    """
    return _USED_MODEL.get(part) or resolve(part)[2]
