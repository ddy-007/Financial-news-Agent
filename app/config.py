"""全局配置：从 .env / 环境变量读取。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- DeepSeek LLM ----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

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

    # ---- 信息量门槛（任一满足即视为「有料」）----
    # 四个信号全不满足 → low_info=True（仍生成报告，仅标记 + 精简）
    info_new_threshold: int = 15          # ① 今日新增新闻数
    info_source_threshold: int = 2        # ②a 多源佐证的 source_count 门槛
    info_sentiment_threshold: float = 0.3  # ②b 情绪强度门槛 |sentiment|
    info_market_threshold: float = 1.0    # ③ 市场异动 |涨跌幅| 门槛（%）
    # 情绪打分只覆盖近 N 天（老新闻不参与研判，无需打分）
    sentiment_score_days: int = 3

    # ---- 采集调度 ----
    news_lookback_days: int = 3
    market_collect_time: str = "17:30"
    sector_collect_time: str = "17:40"   # 板块数据（在行情之后）
    report_time: str = "18:00"
    weekly_report_time: str = "18:30"  # 仅在每周最后一个交易日实际生成


settings = Settings()
