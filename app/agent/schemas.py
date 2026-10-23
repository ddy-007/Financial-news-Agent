"""多专家分析的数据契约。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CriticVerdict(BaseModel):
    """审计必须明确表态；矛盾或空白的驳回结果不能被当作通过。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    is_passed: bool = Field(description="所有事实与数据均有精确依据且未遗漏下行风险才为 true")
    flaws: list[str] = Field(description="具体数据出入、遗漏风险或无依据结论；通过时为空")
    revision_instructions: str = Field(description="要求分析师针对性删改的指令；通过时为空")

    @model_validator(mode="after")
    def coherent_verdict(self):
        if self.is_passed and (self.flaws or self.revision_instructions.strip()):
            raise ValueError("审核通过时不能同时存在缺陷或修订指令")
        if not self.is_passed and (
            not self.flaws or any(not flaw.strip() for flaw in self.flaws)
            or not self.revision_instructions.strip()
        ):
            raise ValueError("审核驳回必须提供具体缺陷及修订指令")
        return self


class ExpertOpinion(BaseModel):
    """4 位分析师共用的输出结构。"""

    expert: str = ""                                     # 宏观/行业/资金面/技术面
    stance: Literal["看多", "中性", "看空"] = "中性"
    score: float = Field(default=0.0, allow_inf_nan=False) # -1(极空) ~ 1(极多)
    confidence: Literal["high", "medium", "low"] = "medium"
    key_points: list[str] = Field(default_factory=list)   # 核心论据
    evidence: list[str] = Field(default_factory=list)     # 引用的新闻标题/数据点
    uncertainties: list[str] = Field(default_factory=list)  # 看不清楚的地方


class RiskOpinion(BaseModel):
    """风险官输出结构（不参与方向加权）。"""

    risk_level: Literal["高", "中", "低"] = "中"
    risks: list[str] = Field(default_factory=list)            # 风险点清单
    counter_arguments: list[str] = Field(default_factory=list)  # 对分析师乐观结论的反驳
    worst_case: str = ""                                      # 最坏情况推演
    blind_spots: list[str] = Field(default_factory=list)      # 分析盲区


class FinalReport(BaseModel):
    """首席策略师输出（兼容既有 MarketReport 字段）。"""

    # —— 沿用既有字段 ——
    title: str = ""                                  # 顶部短标题（8~24字）
    market_summary: str = ""
    sentiment: Literal["偏多", "中性", "偏空"] = "中性"
    confidence: Literal["high", "medium", "low"] = "medium"
    score: float = 0.0
    key_drivers: list[str] = Field(default_factory=list)
    sector_opportunities: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    reference_news: list[str] = Field(default_factory=list)
    # —— 新增字段 ——
    expert_opinions: list[dict] = Field(default_factory=list)
    divergence: float = 0.0
    consensus_note: str = ""
    risk_veto: bool = False
