"""全局配置：从 .env / 环境变量读取。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- DeepSeek LLM（全局默认：各分组未单独配置时回落到这里）----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # ---- 按环节分组的 LLM 配置 ----
    # 每个分组的三个字段都可留空；留空的字段回落到上面的 DEEPSEEK_*。
    # 用途：给不同环节接不同的服务商 / 模型（例如把新闻采集换成便宜或本地的模型），
    #       而研判仍用主力模型。分组对应关系见 app/agent/llm.py 的 _GROUPS。
    #   news   ：每日新闻采集（新闻分类 / 语义判重 / 情绪打分）
    #   expert ：多专家研判（4 位分析师 / 风险官 / 首席 / 兜底 / 评估层）
    #   chat   ：前端问答 Agent
    #   weekly ：周报
    news_api_key: str = ""
    news_base_url: str = ""
    news_model: str = ""
    expert_api_key: str = ""
    expert_base_url: str = ""
    expert_model: str = ""
    chat_api_key: str = ""
    chat_base_url: str = ""
    chat_model: str = ""
    weekly_api_key: str = ""
    weekly_base_url: str = ""
    weekly_model: str = ""

    # ---- 关闭「思考模式」（按环节，可选）----
    # 只对**不需要推理**的环节开：新闻分类 / 判重 / 情绪打分这类任务，
    # 模型花在思考上的时间纯属浪费（实测单批 29.7s -> 8.0s）。
    # ⚠️ 该参数（enable_thinking）是百炼 / qwen 系模型特有的；
    #    分组指向其它服务商时**不要开**，否则请求里会多出对方不认识的参数。
    news_disable_thinking: bool = False
    expert_disable_thinking: bool = False
    chat_disable_thinking: bool = False
    weekly_disable_thinking: bool = False

    # ---- LLM 请求超时（秒）----
    # 不设时 openai SDK 默认 600s，主备都挂时单个 attempt 会拖到 1200s。
    llm_timeout: float = 120.0

    # ---- 备用模型（可选，主模型失败时自动切换）----
    # 三项**全部非空**才启用；任一为空则不启用（行为与未配置时完全一致）。
    # 用途：主模型超时 / 限流 / 5xx / 模型下线 / key 失效时，自动切到备用模型再试一次。
    # 触发条件见 app/agent/llm.py 的 _FALLBACK_EXCEPTIONS。
    #
    # ⚠️ 关于「要不要跨服务商」，2026-09-21 实测后修正过一句**曾经写错的话**：
    #   旧注释说"同一家的限流、欠费是账号级的，换个模型名照样中招"。
    #   **这条对「免费额度」不成立** —— 实测同一账号、同一个 key 下，
    #   `qwen3.7-flash` 报 403「免费额度耗尽」，而 `qwen3.6-plus` 正常可用。
    #   也就是说**免费额度是按模型分配的**，同服务商换个仍有额度的模型是有效的兜底。
    #   （限流/欠费是否账号级**未实测**，不要照搬上面这条结论。）
    llm_fallback_api_key: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""

    # 追加备用模型（**逗号分隔**，与上面三项**共用** key / base_url）。
    # 按顺序排在 `llm_fallback_model` **之后**，主模型和前面的备用都失败时才轮到它。
    # 例：LLM_FALLBACK_MODELS=qwen3.5-plus,qwen-max
    #
    # 为什么用逗号而不是斜杠：主流平台的模型名**本身带斜杠**
    # （OpenRouter 是 `qwen/qwen3.6-plus`、硅基流动是 `Qwen/Qwen3-...`），
    # 用斜杠当分隔符将来必然歧义到没法解析。
    llm_fallback_models: str = ""

    # ---- 本地嵌入 / 重排模型 ----
    bge_m3_model_path: str = "BAAI/bge-m3"
    bge_reranker_path: str = "BAAI/bge-reranker-v2-m3"

    # ---- 存储 ----
    chroma_persist_dir: str = "./data/chroma"
    database_url: str = "sqlite:///./data/app.db"

    # ---- Web 搜索 ----
    tavily_api_key: str = ""

    # ---- 多专家分析 ----
    # 风险官措辞强度 [0,1]，0.5 中性，数值越小越尖锐
    #[0.0, 0.30)  极度尖锐 —— 坚定空头，主动质疑一切乐观结论
    #[0.30, 0.45) 倾向尖锐 —— 主动找逻辑漏洞          ← 当前 0.4 落这里
    #[0.45, 0.55] 中性
    #(0.55, 0.70] 倾向温和
    #(0.70, 1.0]  温和

    risk_intensity: float = 0.4
    # 专家权重（风险官不参与方向加权）
    weight_macro: float = 0.30
    weight_industry: float = 0.25
    weight_capital: float = 0.25
    weight_technical: float = 0.20
    # 分歧度阈值：<low 高度一致；low~high 存在分歧；>high 严重分歧
    divergence_low: float = 0.5
    divergence_high: float = 1.0

    # ---- 综合分（加权平均后的 score）判定阈值 ----
    # **三处共用这一套值**，不许各写各的：
    #   aggregate_node 定调（偏多/中性/偏空）
    #   compute_backtest 判方向（算不算"看多"）
    #   _score_bucket 分档（按情绪分档统计胜率）
    # 历史上它们分别是 ±0.15 与 ±0.1，导致同一份综合分 -0.12 被一处判「看空」、
    # 另一处判「中性」—— 自己跟自己打架。统一后由这两项一处控制。
    score_neutral_band: float = 0.15   # |综合分| < 此值 → 中性（无明确方向）
    score_strong_band: float = 0.5     # |综合分| > 此值 → 强多 / 强空

    # ---- 信息量门槛（任一满足即视为「有料」）----
    # 四个信号全不满足 → low_info=True（仍生成报告，仅标记 + 精简）
    info_new_threshold: int = 15          # ① 今日新增新闻数
    info_source_threshold: int = 2        # ②a 多源佐证的 source_count 门槛
    info_sentiment_threshold: float = 0.3  # ②b 情绪强度门槛 |sentiment|
    info_market_threshold: float = 1.0    # ③ 市场异动 |涨跌幅| 门槛（%）
    # 情绪打分只覆盖近 N 天（老新闻不参与研判，无需打分）
    sentiment_score_days: int = 3

    # ---- 采集调度 ----
    # 新闻采集的**回看天数**：采集器翻页到此天数之前的新闻就停（`news_collector.py`
    # 的 cutoff）。注意它只是「何时停止翻页」的启发式，**不是硬过滤**——同一页里
    # 更老的条目仍会被收下，真正上限是「每个源最多翻 20 页」。
    #
    # 研判时喂给分析师的新闻窗口是另一回事（`graph.py` 里写死的近 1 天），
    # 与这里无关。默认值与 `.env.example`、`EXPERTS_DESIGN.md`（「近 1 天」）保持一致。
    news_lookback_days: int = 1

    # 新闻采集间隔（分钟，2026-09-22 新增，原为写死在 `scheduler.py` 的 `*/30`）。
    #
    # ⚠️ **合法值是固定的**：15 / 20 / 30 / 60 / 120 / 180 / 240 / 360 / 480 / 720（分钟）。
    # 判据是「整点对齐 **且** 节奏恒定」，实现见 `app/collectors/scheduler.py` 的
    # `_interval_ok()`。**光"是 60 的倍数"不够**，两个反例：
    #   · 300（看着像每 5 小时）→ 24 % 5 != 0，一天末尾只剩 4 小时（实测 {240, 300}）
    #   · 1440（每天一次）     → `hour="*/24"` 直接抛 ValueError，**后端起不来**
    # 其余值一律回退为 60 并在日志里告警，不会静默变成另一个间隔。
    #
    # **为什么必须避开报告时刻**：四个报告任务全挤在 17:30–19:30
    # （行情 17:30 / 板块 17:40 / 日报 18:15 / 周报 19:30），而新闻采集每轮要跑
    # 4~20 分钟，且写的是**未开 WAL 的 SQLite**（写的时候读会被挡住，pysqlite
    # 默认 5 秒 busy timeout，超了就是 `database is locked`）。
    #
    # 2026-09-22 由 30 调为 **60 整点**：上面四个时刻**没有一个是整点**，
    # 原本 17:30（撞行情）和 18:30（撞周报）两处同刻撞车就此消失。
    # 副作用：每轮窗口 45 → 75 分钟，冗余 +50% → +25%；时效性最多晚 60 分钟。
    news_interval_minutes: int = 60

    # 增量水位线的**重叠窗口**（分钟，2026-09-22 新增；同日由 30 调为 15）。
    #
    # 下一轮的停止条件 = `该源上轮水位线 - 本值`，而不是「距现在 N 天」。
    # 留重叠是**故意冗余**：源站可能有发布延迟 —— 一条新闻发布后过一会儿才出现在
    # feed 里，若不留重叠，那条就永远取不到了（设计 §13 的「增量引入新闻缺口」）。
    #
    # **每轮窗口 = `news_interval_minutes` + 本值**，当前 60 + 15 = **75 分钟**。
    # 本值若与间隔等长，每轮就有**一半条目是上轮刚抓过的** —— 那些同样要过 LLM 分类。
    # 实测佐证（2026-09-22 22:20 那轮，当时间隔 30 / 窗口 60）：抓 290 条 →
    # 相关 218 / 丢弃 72 / 合并 164 / **真正新增只有 54 条（19%）**。
    #
    # ⚠️ 代价是**漏采裕度变小**。若日志/报告里出现"新闻断档"迹象，把它调回去即可。
    # 想进一步省，可继续下调，但要先确认源站的发布延迟量级。
    news_overlap_minutes: int = 15

    # 单源单轮最多翻多少页（2026-09-22 新增，原为写死的 20）。
    # 它是**防死循环的上限**，不是正常结束条件 —— 撞到它说明该源有积压，
    # 采集层会置 `SourceResult.truncated=True` 并记告警（见设计 §9.1-④）。
    # 注意各源每页条数不同（新浪/东财 50、财联社 20），所以同一个数字对应的容量不同。
    news_max_pages: int = 20

    market_collect_time: str = "17:30"
    sector_collect_time: str = "17:40"   # 板块数据（在行情之后）
    report_time: str = "18:00"
    # 周报时间（仅每周最后一个交易日实际生成）。
    # 2026-09-22 由 18:30 改为 19:30：日报 18:15 起跑，原先只隔 15 分钟 ——
    # 日报（四位分析师 + 风险官 + 首席）跑超 15 分钟的话，周报就**取不到当天那份日报**。
    # 也不能取 19:00：新闻采集改成整点后，`minute="0"` 会在 19:00 同样触发。
    # 19:30 距日报 75 分钟、距 19:00 那轮新闻 30 分钟，两边都不撞。
    weekly_report_time: str = "19:30"


settings = Settings()
