"""5 位专家 Agent：4 位领域分析师 + 1 位风险官。

设计要点：
- 专家是「被调用的分析函数」，不是自主循环 agent（混合 Pipeline 形态）
- 定性结论由 LLM 产出，定量定档（加权分/分歧度/置信度）由代码计算
- 风险官措辞强度由 RISK_INTENSITY 配置驱动，不写死
"""
from __future__ import annotations

import json
import re

from loguru import logger

from app.agent.schemas import ExpertOpinion, RiskOpinion
from app.config import settings

# ================= 公共工具 =================
def extract_json(text: str):
    """从 LLM 输出中容错提取 JSON（数组或对象）。"""
    text = (text or "").strip()
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pat in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    return None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)]


def _clamp(v, lo=-1.0, hi=1.0) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _to_opinion(name: str, data: dict) -> ExpertOpinion:
    """安全构造 ExpertOpinion（容错 LLM 输出的非法值）。"""
    stance = str(data.get("stance", "中性")).strip()
    if stance not in ("看多", "中性", "看空"):
        stance = "中性"
    conf = str(data.get("confidence", "medium")).strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    return ExpertOpinion(
        expert=name,
        stance=stance,
        score=_clamp(data.get("score", 0.0)),
        confidence=conf,
        key_points=_as_list(data.get("key_points")),
        evidence=_as_list(data.get("evidence")),
        uncertainties=_as_list(data.get("uncertainties")),
    )


def _require_dict(text: str, who: str) -> dict:
    """解析 LLM 输出并要求为 dict，否则抛错（供重试）。"""
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError(f"{who}输出无法解析为 JSON")
    return data


def _to_risk_opinion(data: dict) -> RiskOpinion:
    lvl = str(data.get("risk_level", "中")).strip()
    if lvl not in ("高", "中", "低"):
        lvl = "中"
    return RiskOpinion(
        risk_level=lvl,
        risks=_as_list(data.get("risks")),
        counter_arguments=_as_list(data.get("counter_arguments")),
        worst_case=str(data.get("worst_case", "") or ""),
        blind_spots=_as_list(data.get("blind_spots")),
    )


# ================= 风险官措辞强度 =================
INTENSITY_BANDS = [
    (0.30, "极度尖锐：扮演坚定的空头，主动质疑一切乐观结论，不留情面地指出逻辑漏洞。"),
    (0.45, "倾向尖锐：主动寻找逻辑漏洞，不轻易接受乐观结论，对每条多头观点提出质疑。"),
    (0.55, "中性：客观陈述风险，不预设立场。"),
    (0.70, "倾向温和：提示风险，但认可合理的乐观论据。"),
    (1.01, "温和：仅做常规风险提示，措辞克制。"),
]


def get_intensity_instruction(intensity: float | None = None) -> str:
    """按 RISK_INTENSITY 返回语气指令（数值越小越尖锐，0.5 中性）。"""
    v = settings.risk_intensity if intensity is None else intensity
    for threshold, text in INTENSITY_BANDS:
        if v < threshold:
            return text
    return INTENSITY_BANDS[-1][1]


# ================= Prompt 模板 =================
_JSON_SPEC = """严格只输出 JSON（不要输出任何其他文字）：
{{
  "stance": "看多/中性/看空",
  "score": 0.0,
  "score_band": "强多/偏多/中性/偏空/强空",
  "score_reason": "为什么落在这个档（一句话，指向具体依据）",
  "confidence": "high/medium/low",
  "key_points": ["核心论据（须附依据）"],
  "evidence": ["引用的新闻标题或数据点"],
  "uncertainties": ["本维度看不清楚的地方"]
}}

【score 打分锚点 —— 四位分析师统一，务必对齐】
  +0.5 ~ +1.0    强多 —— 多重、独立、可验证的利好叠加
  +0.15 ~ +0.5   偏多 —— 有明确积极因素，但存在明显不确定性
  -0.15 ~ +0.15  中性 —— 信号混杂、多空相抵，或信息不足
  -0.5 ~ -0.15   偏空 —— 有明确负面因素
  -1.0 ~ -0.5    强空 —— 多重利空叠加

打分要求：
1. `score` 必须与 `score_band` 一致（band=偏多 → score 落在 +0.15~+0.5），
   并在 `score_reason` 里说清依据
2. **不要因为"说不准"就往零缩**。信息确实不足时可以打中性档，
   但要在 `uncertainties` 里说明缺什么；**倾向明确时必须给出对应分数**
3. 这套刻度是四位分析师共用的 —— **你的 0.5 和别人的 0.5 必须是同一个意思**
4. **对称性要求**：锚点是对称的 —— N 条独立、可验证的利好，与 N 条同等强度的利空，
   评分绝对值应当相当。若你发现自己在看多方向明显更保守，请在 `uncertainties`
   里说明理由（那本身是一条信息）；但**不要默认这么做**
"""

_COMMON_RULES = """分析纪律：
1. 只基于给出的材料，不得编造事实或数据
2. 每条论据必须注明依据（新闻标题或数据点）
3. 数据缺失时如实说明"数据不足"，不要凭空推断
4. 明确说出你不确定的地方

禁止清单（四位分析师共用，违反即视为不合格输出）：
1. **禁泛化表述** —— 不得只写"科技板块""流动性宽松"这类笼统话，
   必须落到具体板块名、具体数据、具体政策名
2. **禁无依据断言** —— 不得给出材料里找不到支撑的结论
3. **禁动量外推** —— 不得把"今天涨得好/跌得多"直接推成"明天继续"，
   那只是动量外推；要说明资金流向的**结构性含义**（钱在从哪撤、往哪去）
4. **禁讨好式中性** —— 不得因为"说不准"就一律打 0 分写成中性。
   信息确实不足时可以取中性档，但必须在 uncertainties 里说明**缺什么**；
   倾向明确时必须给出对应分数，不要往零附近缩"""

MACRO_PROMPT = """你是一位宏观分析师，负责判断货币/财政政策与外部环境对 A 股流动性的影响。

【今日宏观/政策类新闻】
{macro_news}

【综合类新闻】（跨类别，供参考）
{comprehensive_news}

【主要指数行情】
{market_snapshot}

【美股（外部环境）】
{us_market}

关注点：降准降息、逆回购、LPR、财政投放、美联储、汇率、经济数据。
特别注意：区分「政策信号」与「政策落地」，不要把表态直接当成利好。

{common_rules}

{json_spec}
"""

INDUSTRY_PROMPT = """你是一位行业分析师，负责识别板块轮动方向与具体机会。

【今日行业/公司类新闻】
{industry_news}

【板块实际表现】（市场真实反应，非新闻；日期见数据首行）
{sector_summary}

【综合类新闻】（跨类别，供参考）
{comprehensive_news}

【主要指数行情】
{market_snapshot}

关注点：产业链涨价/降价、政策扶持、龙头公司动向、题材热点。

**使用板块数据的要求**：
1. 板块表现是「市场**已发生**的实际反应」，请用它和新闻**交叉验证**：
   · 新闻利好但板块下跌 → 预期已消化，或消息未被市场认可
   · 新闻未提但板块大涨 → 可能存在未被捕捉到的信息
2. **不要把「今天涨得好」直接外推成「明天看好」**——那只是动量外推，价值有限。
   请说明资金流向的**结构性含义**（钱在从哪撤、往哪去）。

{common_rules}

注意：key_points 中必须给出**具体的板块名称**，禁止"科技板块"这类泛化表述。

{json_spec}
"""

CAPITAL_PROMPT = """你是一位资金面分析师，负责判断资金态度（增量入场 / 存量博弈 / 撤离）。

【今日资金/市场类新闻】
{capital_news}

【综合类新闻】（跨类别，供参考）
{comprehensive_news}

【主要指数行情】
{market_snapshot}

关注点：北向资金净流入/流出、融资余额、主力资金、成交量能、ETF 申赎。

{common_rules}

注意：资金数据缺失时必须如实标注"资金数据不足"，**这是最容易产生幻觉的维度**。

{json_spec}
"""

TECHNICAL_PROMPT = """你是一位技术面分析师，负责从价格/量能形态判断短期多空。

【技术指标】（已由程序计算，请直接采用，不要自行计算）
{indicators}

【主要指数行情】
{market_snapshot}

【综合类新闻】（跨类别，供参考）
{comprehensive_news}

关注点：均线多空排列、量能变化、涨跌动量、RSI、支撑/压力位。

{common_rules}

注意：结论要可验证，例如"站上/跌破 MA20"这种明确表述，避免模糊措辞。

{json_spec}
"""

RISK_PROMPT = """你是一位专职风险官。{intensity_instruction}

你的价值在于找出其他人忽略的风险，而**不是附和**。

【全部今日新闻】
{all_news}

【其他分析师的结论】
{other_opinions}

你的职责：
1. 系统性找出利空与风险点
2. 针对上述分析师的乐观结论，**逐条提出反驳或质疑**
3. 推演最坏情况
4. 指出当前分析的盲区

**`risk_level` 评的是「形势本身有多危险」，与「分析师有多乐观」无关。**
反驳得多 ≠ 风险高：分析师集体看多时你要挑的刺自然多，但那说明的是
你的工作量，不是市场的危险程度。请按下面的刻度**独立**判断：

  "高" —— 存在**可验证的**、可能造成显著回撤的利空
          （政策转向、资金持续净流出、技术破位、外部冲击）
  "中" —— 有值得警惕的风险点，但影响可控，或已有对冲
  "低" —— 未发现明确利空。**日常波动、个股减持、增速小幅回落、
          估值偏高等常态噪音，属于「无明确利空」，不构成「中」或「高」**

**不要为了显得尽责而抬高等级。** 该给"低"就给"低" ——
把"低"也说成"高"，会让降级机制失去区分度，那是失职而不是尽责。

禁止**无依据的**表述：说"风险可控"要给出依据（哪条数据/新闻支持），
但**若确实未发现明确利空，就如实给"低"并说明**；
同理，说"高风险"也必须指出可验证的利空，不能只凭语气。
每条风险须有依据，反驳要具体针对某位分析师的观点。

严格只输出 JSON（不要输出其他文字）：
{{
  "risk_level": "高/中/低",
  "risks": ["风险点（附依据）"],
  "counter_arguments": ["针对某位分析师具体观点的反驳"],
  "worst_case": "最坏情况推演",
  "blind_spots": ["当前分析的盲区"]
}}
"""


# ================= 专家调用 =================
def _invoke_expert(llm, name: str, prompt: str, *, part: str) -> ExpertOpinion:
    """调用一位分析师，返回结构化观点。

    **重试覆盖网络与解析两步**——LLM 输出不稳时重试往往能拿到合规结果，
    这样「专家缺席」这个降级点基本不会触发。

    `part` 必填：重试次数**按分组**算（备用是逐分组挂的），而 `llm` 是注入的，
    两者必须来自同一个来源。写死在函数里的话，将来有人传入别组的客户端时
    **不会报错**，只会静默用错那个分组的重试次数。
    """
    from app.agent.llm import llm_retry_times
    from app.retry import call_with_retry

    def _attempt() -> ExpertOpinion:
        resp = llm.invoke(prompt)
        data = extract_json(resp.content)
        if not isinstance(data, dict):
            raise ValueError(f"{name}分析师输出无法解析为 JSON")
        return _to_opinion(name, data)

    op = call_with_retry(_attempt, retry_label=f"{name}分析师",
                         retry_times=llm_retry_times(part=part))
    logger.info(f"[{name}] {op.stance} score={op.score:.2f} conf={op.confidence}")
    return op


def run_macro_expert(llm, ctx: dict, *, part: str) -> ExpertOpinion:
    return _invoke_expert(llm, "宏观", MACRO_PROMPT.format(
        macro_news=ctx.get("macro_news", "无"),
        comprehensive_news=ctx.get("comprehensive_news", "无"),
        market_snapshot=ctx.get("market_snapshot", "无"),
        us_market=ctx.get("us_market", "无"),
        common_rules=_COMMON_RULES, json_spec=_JSON_SPEC,
    ), part=part)


def run_industry_expert(llm, ctx: dict, *, part: str) -> ExpertOpinion:
    return _invoke_expert(llm, "行业", INDUSTRY_PROMPT.format(
        industry_news=ctx.get("industry_news", "无"),
        sector_summary=ctx.get("sector_summary", "无"),
        comprehensive_news=ctx.get("comprehensive_news", "无"),
        market_snapshot=ctx.get("market_snapshot", "无"),
        common_rules=_COMMON_RULES, json_spec=_JSON_SPEC,
    ), part=part)


def run_capital_expert(llm, ctx: dict, *, part: str) -> ExpertOpinion:
    return _invoke_expert(llm, "资金面", CAPITAL_PROMPT.format(
        capital_news=ctx.get("capital_news", "无"),
        comprehensive_news=ctx.get("comprehensive_news", "无"),
        market_snapshot=ctx.get("market_snapshot", "无"),
        common_rules=_COMMON_RULES, json_spec=_JSON_SPEC,
    ), part=part)


def run_technical_expert(llm, ctx: dict, *, part: str) -> ExpertOpinion:
    return _invoke_expert(llm, "技术面", TECHNICAL_PROMPT.format(
        indicators=ctx.get("indicators", "无"),
        market_snapshot=ctx.get("market_snapshot", "无"),
        comprehensive_news=ctx.get("comprehensive_news", "无"),
        common_rules=_COMMON_RULES, json_spec=_JSON_SPEC,
    ), part=part)


def run_risk_officer(llm, ctx: dict, *, part: str) -> RiskOpinion:
    """风险官：唯一能看到其他专家结论的角色。"""
    prompt = RISK_PROMPT.format(
        intensity_instruction=get_intensity_instruction(),
        all_news=ctx.get("all_news", "无"),
        other_opinions=ctx.get("other_opinions", "无"),
    )
    from app.agent.llm import llm_retry_times
    from app.retry import call_with_retry

    def _attempt() -> RiskOpinion:
        resp = llm.invoke(prompt)
        data = extract_json(resp.content)
        if not isinstance(data, dict):
            raise ValueError("风险官输出无法解析为 JSON")
        return _to_risk_opinion(data)

    risk = call_with_retry(_attempt, retry_label="风险官",
                           retry_times=llm_retry_times(part=part))
    logger.info(f"[风险官] 风险等级={risk.risk_level} 风险点={len(risk.risks)}")
    return risk


# ================= 首席策略师 =================
CHIEF_PROMPT = """你是一位首席策略师，负责汇总多位分析师的观点并撰写最终研判报告。

【今日行情】
{market_snapshot}

【专家观点】
{expert_opinions}

【风险官意见】
{risk_opinion}

【已由程序计算好的量化结果】（请直接采用，不要自行更改）
- 加权综合分：{weighted_score}
- 分歧度：{divergence}（{divergence_desc}）
- 最终置信度：{final_confidence}
- 风险官是否触发降档：{risk_veto}
{info_hint}{fail_hint}{data_hint}

撰写要求：
1. market_summary：综合多方观点，**并体现主要分歧**，不要只挑乐观的说
2. key_drivers：核心驱动因素，每条注明依据
3. sector_opportunities：具体板块机会
4. risks：**必须完整包含风险官提出的所有风险点，不得删减或淡化**
5. reference_news：引用的新闻标题
6. consensus_note：说明专家之间的分歧所在，以及为什么最终这样定调

严格只输出 JSON（不要输出任何其他文字）：
{{
  "market_summary": "大盘综述",
  "key_drivers": ["驱动因素（附依据）"],
  "sector_opportunities": ["板块机会"],
  "risks": ["风险提示"],
  "reference_news": ["引用的新闻标题"],
  "consensus_note": "对分歧的说明"
}}
"""


def run_chief(llm, ctx: dict, opinions: list[ExpertOpinion],
              risk: RiskOpinion, quant: dict, *, part: str) -> dict:
    """首席策略师：撰写叙述性内容；定量结论由 quant 提供。"""
    expert_text = "\n".join(
        f"【{o.expert}】{o.stance}（score={o.score:+.2f}，置信度={o.confidence}）\n"
        + "\n".join(f"  · {p}" for p in o.key_points)
        for o in opinions
    ) or "无"

    risk_text = (
        f"风险等级：{risk.risk_level}\n"
        f"风险点：\n" + "\n".join(f"  · {r}" for r in risk.risks)
        + ("\n反驳意见：\n" + "\n".join(f"  · {c}" for c in risk.counter_arguments)
           if risk.counter_arguments else "")
        + (f"\n最坏情况：{risk.worst_case}" if risk.worst_case else "")
    )

    # 信息量偏低时，明确提示"低调处理"——既不硬凑字数，也不假装有大行情
    info = quant.get("info_level") or {}
    if info.get("low_info"):
        info_hint = (
            f"\n【今日信息量提示】当日四个信号均未触发（{info.get('reason', '')}），"
            "市场信息量较低。请在 market_summary 中如实说明「今日无重大消息」，"
            "**篇幅可以精简，不要为凑字数而堆砌内容**；风险提示仍须完整保留。"
        )
    else:
        info_hint = ""

    # 专家缺席：必须让首席知道，否则报告会写得像全员参与（且权重已被静默改变）
    failed = quant.get("failed_experts") or []
    if failed:
        fail_hint = (
            f"\n【专家缺席提示】以下专家本次分析**失败、未参与研判**："
            f"{'、'.join(failed)}。请在 consensus_note 中**明确说明**这一情况，"
            "不要让报告看起来像全员参与。"
        )
    else:
        fail_hint = ""

    # 数据陈旧：不得把旧闻表述为"今日"消息
    fr = quant.get("data_freshness") or {}
    if fr.get("stale"):
        cats = "、".join(fr.get("stale_categories") or []) or "部分类目"
        data_hint = (
            f"\n【数据时效提示】{cats} 类新闻不足，本次研判使用的是"
            f"**截至 {fr.get('stale_data_date') or '更早'}** 的数据（非当日）。"
            "请在 market_summary 中**明确说明数据时效**，"
            "**不要表述为「今日」消息**。"
        )
    else:
        data_hint = ""

    prompt = CHIEF_PROMPT.format(
        market_snapshot=ctx.get("market_snapshot", "无"),
        expert_opinions=expert_text,
        risk_opinion=risk_text,
        weighted_score=f"{quant['weighted_score']:+.2f}",
        divergence=f"{quant['divergence']:.2f}",
        divergence_desc=quant["divergence_desc"],
        final_confidence=quant["final_confidence"],
        risk_veto="是" if quant["risk_veto"] else "否",
        info_hint=info_hint,
        fail_hint=fail_hint,
        data_hint=data_hint,
    )
    from app.agent.llm import llm_retry_times
    from app.retry import call_with_retry

    data = call_with_retry(
        lambda: _require_dict(llm.invoke(prompt).content, "首席策略师"),
        retry_label="首席策略师",
        retry_times=llm_retry_times(part=part),
    )

    # 强制并入风险官的全部风险点（不得被 LLM 过滤）
    risks = list(risk.risks)
    for r in _as_list(data.get("risks")):
        if r not in risks:
            risks.append(r)

    return {
        "market_summary": str(data.get("market_summary", "")),
        "key_drivers": _as_list(data.get("key_drivers")),
        "sector_opportunities": _as_list(data.get("sector_opportunities")),
        "risks": risks,
        "reference_news": _as_list(data.get("reference_news")),
        "consensus_note": str(data.get("consensus_note", "")),
    }
