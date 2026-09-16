# 评估层 Spec（改写版 v3）

> 版本：v3.0　日期：2026-09-14　状态：待实现
> 本文件是对初版 spec 的改写 + 扩充。改了什么见 §0.2。

---

## 0. 背景

### 0.1 现状事实（2026-09-14 实测核实）

> ⚠️ 初版 spec 的「现状事实」已过时，以下为**实测**结果。执行前请以此为准。

| 项 | 实测值 |
|----|--------|
| `EXPERTS_DESIGN.md` | **v1.2，多专家架构已实现并验证通过**（非"v1.0 未实现"） |
| `report` 表记录数 | **3 条**（2026-09-06 ×2、2026-09-14 ×1） |
| `report` 表字段 | id, date, **report_type**, title, content, sentiment, confidence, score, model, created_at, **expert_opinions, divergence, risk_veto, low_info, data_stale** |
| 最新 report.content 顶层字段 | **18 个**（含 expert_opinions / risk_opinion / divergence / risk_veto / consensus_note / low_info / data_freshness / env） |
| 旧 report（9/6）content | 8 个旧字段（无多专家信息） |
| `risk_veto` 字段 | **已存在**（非"待迁移"） |
| `market_data` | 242 条（已补采至约 500 条，含 70 天历史） |
| `news.category` 分布 | 公司365 / 行业356 / 国际325 / 宏观186 / 政策134 / 资金75 / **策略1**（异常残留） |
| 上证 symbol | `sh000001` |
| `scipy` | **1.17.1 已安装** ✓（Mann-Whitney U 可用） |
| 专家 temperature | 分析师 0.2 / 风险官 0.3 / 首席 0.2 |

### 0.2 与初版的差异

| # | 初版 | 本版 | 原因 |
|---|------|------|------|
| 1 | 前提称架构"未实现"、字段"待迁移" | 更正为已实现、字段已存在 | 前提错误会误导实现者 |
| 2 | A1 匹配窗口 `[date-1天, date]` | **全表匹配 + 单独统计"超窗"** | ⚠️ 原窗口会 100% 误报，见 §A1 |
| 3 | A1 用硬包含判命中 | 硬包含 **或** `difflib` 相似度 ≥ 0.6 | 原始口径把"转述"误判为"编造" |
| 4 | A2 未指明标的范围 | 先解析主语，未解析则标记 | "命中任一即算"= 7 次机会，检验力过弱 |
| 5 | A3 类别覆盖混在 agent 评估里 | **移出**为独立的阶段 Q | 它检验采集质量，非 agent 产出 |
| 6 | 无风险等级分布检查 | **新增 A3** | 小样本下唯一可观测的退化信号 |
| 7 | API 路径 `/api/reports/evaluation` | `/api/v1/reports/evaluation` | 现有路由前缀是 `/api/v1/reports` |
| 8 | **完全没有瞬时检验** ⭐ | **新增阶段 S（5 项）** | 见 §1.1，这是本次最大扩充 |
| 9 | — | **再补 S6 风险官相关性 / S7 输出多样性** | 补齐 §1.2 失效模式覆盖表的两个空白 |

---

## 1. 要解决的问题

多专家架构中，有两个角色的产出不进入任何**可证伪**的检验通道：

- **风险官**：5 个输出字段中只有 `risk_level` 有机器可观测效果（仅"高"触发置信度降档 + `risk_veto=True`），其余只供人阅读。
- **首席策略师**：5 个 LLM 产出字段全是文本，对系统零数值影响。

**后果**：无法迭代（无反馈信号）、无法归因（故障不可定位）、无法删除（提不出有证据的理由）。

### 1.1 验证的两个正交维度 ⭐（本版新增的核心框架）

初版 spec 的**全部检验都属于「纵轴检验」**，都需要时间序列、都要等几十份报告。
但验证这两个角色其实有两个**正交维度**：

| 维度 | 问的问题 | 需要什么 | 现在能做吗 |
|------|---------|---------|-----------|
| **纵轴检验**（阶段 A/B） | 它说得**对吗**？ | 几十份报告 + 次日行情 | ❌ 需数周～数月 |
| **瞬时检验**（阶段 S） | 它在**干活吗**？ | 只需当下的输入输出 | ✅ **现在就能做** |

**为什么必须先做瞬时检验**：它是纵轴检验的**前提**。若风险官根本没在响应数据
（输出固定套话），测它的"准确率"毫无意义——那只是给一个**空转的组件**算统计。
**先证明它在工作，再证明它工作得好。**

**优先级**：阶段 S > 阶段 A > 阶段 B。

### 1.2 失效模式覆盖表 ⭐（本版新增）

先枚举两个角色可能的**失效模式**，再检查哪些没有检测手段：

| 角色 | 失效模式 | 检测手段 |
|------|---------|---------|
| 风险官 | 永远判"高"（降档常态化） | ✅ A3 |
| 风险官 | 泛泛而谈，不针对具体观点 | ✅ S3 |
| 风险官 | **输出与当天新闻无关（套话）** | ✅ **S6**（本版新增） |
| 风险官 | 过度悲观（系统性误伤看多） | ✅ B2 |
| 首席 | 编造新闻引用 | ✅ A1 |
| 首席 | 编造数字 | ✅ A2 |
| 首席 | 淡化专家分歧 | ✅ S4 |
| 首席 | 措辞与定调不符 | ✅ S5 |
| 首席 | **输出模板化（每天雷同）** | ✅ **S7**（本版新增） |
| 两者 | 对输入不敏感（空转） | ✅ S1 |
| 两者 | 输出不稳定 | ✅ S2 |

**仍未覆盖的空白**（诚实声明）：

| 空白 | 为什么没做 |
|------|-----------|
| 风险官**"该说没说"**（漏掉真实风险的假阴性） | 需要外部 ground truth 才能判定，属纵轴检验 |
| 首席**"分析深度不足"**（结论对但论证浅） | 机器难以判定，需人工评审 |
| 两者的**置信度校准**（过度自信/过度谦逊） | 需要准确率数据，属阶段 B |

> 方法论意义：**先枚举失效模式，再问"每个模式有检测吗"**，比零散地想检查项更不容易漏。
> 上表同时暴露了"哪些是机器可判的、哪些必须靠人"。

---

## 2. 目标

**只新增评估层，不改动多专家架构本身。** 让上述两个角色变得可测量、可证伪。

---

## 3. 硬约束（不要做）

1. 不修改 `EXPERTS_DESIGN.md`
2. 不新增或删除 agent，不修改任何 prompt 模板
3. 不修改 `compute_backtest()` 的任何行为与返回值结构
4. 不修改 `run_daily_pipeline()` 的对外签名
5. **所有检验均为只读**，不得写回 `report` / `news` / `market_data` 的任何字段
6. 用增量编辑，不整体重写既有文件
7. 不得为了让检验通过而调整 prompt 或修改历史数据

> 注：阶段 S 会**直接构造 ctx 调用专家函数**（绕过数据库读），这不违反约束 5——
> 它不写入任何表。但它会产生 LLM 调用开销，故**默认不进每日巡检**，见 §9.3。

---

# 阶段 S —— 瞬时检验（最高优先级，现在就能做）

## S1 `check_sensitivity(db=None, expert="行业")` → dict ⭐**最有价值**

**目的**：敏感性 / 空转检测——Agent 是真的在"读数据"，还是在输出固定模板？

**方法**：构造 3 组**极端合成输入**，直接调用专家函数，看是否朝对应方向响应。
**不读写数据库**——ctx 为手工构造的 dict。

**测试夹具**：

| 场景 | 输入特征 | 期望响应 |
|------|---------|---------|
| `bullish` | 全利好新闻（降准、新规）+ 行情涨 2.5% | `score > 0.3` |
| `bearish` | 全利空新闻（加息、监管处罚）+ 行情跌 2.5% | `score < -0.3` |
| `empty` | 全部为"无" | `abs(score) <= 0.2`（应诚实说数据不足） |

**判定**：
- `bullish` 未 > 0.3 **或** `bearish` 未 < -0.3 → 追加 `"flag":"agent 可能未在响应数据（空转）"`
- `empty` 的 `abs(score) > 0.2` → 追加 `"flag":"数据缺失时仍在编造结论"`

**返回**：
```json
{"check":"sensitivity","status":"ok","expert":"行业",
 "scenarios":[{"name":"bullish","score":0.65,"stance":"看多","pass":true},
              {"name":"bearish","score":-0.70,"stance":"看空","pass":true},
              {"name":"empty","score":0.0,"stance":"中性","pass":true}],
 "passed":3,"total":3}
```

**关键价值**：**不需要任何历史数据**，能立刻暴露"agent 是死的"这种致命问题。

---

## S2 `check_reproducibility(db, runs=3, expert="行业")` → dict

**目的**：同一输入跑 N 次，测结论稳定性。**这是所有下游评估的地基**——
若系统自身输出就不稳定，`compute_backtest` 测出的"准确率"、B1 测出的"预测力"，
测的都是噪声。

**方法**：
- 用**当前真实数据**构造一次 ctx（复用 `prepare_node` 的取数逻辑，只读）
- 用同一 ctx 调用同一专家 `runs` 次
- 统计 `score` 的均值/标准差/极差，以及 `stance` 的翻转情况

**判定**：
- `score` 标准差 > 0.3 **或** `stance` 出现翻转（如 中性↔看多）→
  追加 `"flag":"输出不稳定，下游统计结论不可靠"`

**返回**：
```json
{"check":"reproducibility","status":"ok","expert":"行业","runs":3,
 "scores":[0.05,0.10,-0.05],"std":0.06,"range":0.15,
 "stances":["中性","中性","中性"],"stance_flips":0,"stable":true}
```

---

## S3 `check_risk_specificity(db, limit=90)` → dict

**目的**：风险官是在"针对具体观点反驳"，还是在"泛泛谈风险"？

**方法**：读取每份报告的 `risk_opinion.counter_arguments`，
检查每条是否**点名**了某位专家（`宏观` / `行业` / `资金面` / `技术面`）。

**计算**：
- `specificity_rate` = 点名的反驳条数 / 总反驳条数
- 实测首份报告表现良好：2 条反驳均点名【资金面】，并指出了
  "南向资金买的是港股，与 A 股增量资金无直接关系"这类逻辑错误 ✓

**判定**：
- `counter_arguments` 为空 → `status="skipped"` + reason
- `specificity_rate < 0.5` → 追加 `"flag":"反驳泛化，未针对具体观点（退化为风险朗读机）"`

**返回**：
```json
{"check":"risk_specificity","status":"ok","reports_scanned":1,
 "total_arguments":2,"specific":2,"specificity_rate":1.0}
```

---

## S4 `check_chief_faithfulness(db, limit=90)` → dict

**目的**：首席是否在**淡化**专家分歧与风险官意见？

**两个子指标**：

**① 风险留存率 `risk_retention`**（护栏验证）
`graph.py::run_chief()` 已在**代码层强制并入**风险官的全部风险点（LLM 无法过滤）。
本检查验证该护栏**是否真的生效**：`risks` 字段应包含风险官 `risks` 的全部条目。
- 期望 = 1.0；**若 < 1.0 说明代码护栏失效**，属严重问题

**② 分歧披露率 `divergence_disclosed`**
对 `divergence > divergence_high(1.0)` 的报告（即"严重分歧"），
检查 `consensus_note` 或 `market_summary` 是否真的**提到**了分歧
（关键词：`分歧` / `不一致` / `矛盾` / `争议`）。

**判定**：
- `risk_retention < 1.0` → `"flag":"风险官意见未被完整并入（护栏失效）"`
- `divergence_disclosed < 1.0` → `"flag":"首席淡化了专家分歧"`

**返回**：
```json
{"check":"chief_faithfulness","status":"ok","reports_scanned":1,
 "risk_retention":1.0,"divergence_cases":0,"divergence_disclosed":null}
```

---

## S5 `check_internal_consistency(db, limit=90)` → dict

**目的**：首席**自身输出**是否自洽——「说一套、单据一套」。

**方法**：用**情感词表**（不调 LLM，零成本）判断 `market_summary` 的措辞倾向，
与代码算出的 `sentiment` 比对。

- 看多词表：`上涨/反弹/利好/机会/走强/回暖/乐观/企稳/向好`
- 看空词表：`下跌/回调/风险/承压/走弱/悲观/下行/抛售/谨慎`

**计算**：`tone = 看多词数 - 看空词数`，与 `sentiment`（偏多/中性/偏空）方向比对。

**判定**：`consistency_rate < 0.7` → 追加 `"flag":"市场综述措辞与定调不一致"`

**返回**：
```json
{"check":"internal_consistency","status":"ok","reports_scanned":3,
 "checked":3,"consistent":3,"consistency_rate":1.0,
 "inconsistent":[{"date":"...","sentiment":"偏空","tone":"偏多"}]}
```

---

## S6 `check_risk_grounding(db, limit=90, threshold=0.5)` → dict ⭐**（新增）**

**目的**：检查风险官的风险点**是否与当天新闻相关**——防止它退化为输出套话
（"估值偏高""地缘风险""流动性风险"这类与当天新闻无关的通用风险）。

**背景**：这是 LLM 最典型的退化方式之一——**放弃阅读输入，直接调取先验**。
它比"判错方向"更隐蔽：套话读起来永远合理，但**信息量为零**。

**方法**（用本地 bge-m3，无额外成本）：
1. 构造**参考语料**：该报告日期窗口内的新闻文本 + 该报告自身的 `expert_opinions` 文本
   （近似重建风险官的输入——因其原始输入未持久化）
2. 对每条 risk，用 bge-m3 嵌入后计算与参考语料中任一条的**最大余弦相似度**
3. `grounded = max_sim >= threshold`（默认 0.5）

**计算**：`grounding_rate` = grounded 条数 / 总风险条数

**判定**：`grounding_rate < 0.6` → 追加 `"flag":"风险点与当天新闻关联弱，疑似套话"`

**返回**：
```json
{"check":"risk_grounding","status":"ok","reports_scanned":1,
 "total_risks":10,"grounded":8,"grounding_rate":0.8,
 "ungrounded":[{"date":"...","risk":"...","max_sim":0.31}]}
```

**已知边界**：部分风险**合理地**不源于新闻——例如"技术面跌破均线"来自价格数据、
"资金面承压"来自分析师结论。故参考语料**同时包含 `expert_opinions` 文本**，
且 `grounding_rate` 偏低只作为**人工复核的触发信号**，不直接判定为故障。

---

## S7 `check_output_diversity(db, limit=90, window=5)` → dict ⭐**（新增）**

**目的**：检测**模板化输出**——连续多天的报告高度雷同，说明首席在套模板而非分析当天数据。

**方法**（`difflib`，零成本、确定性、无 LLM 调用）：

| 指标 | 计算 |
|------|------|
| `summary_similarity` | 相邻报告 `market_summary` 的平均 `SequenceMatcher` 相似度 |
| `drivers_overlap` | 相邻报告 `key_drivers` 的 Jaccard 重合率 |
| `stance_diversity` | 每份报告内 4 位专家 `stance` 的多样性（不同取值数 / 4） |

**判定**：
- 报告数 < 3 → `status="insufficient_data"`（构不成对比对）
- `summary_similarity > 0.7` → 追加 `"flag":"市场综述高度雷同，疑似模板化输出"`
- 报告数 ≥ 10 **且** `stance_diversity` 均值 < 0.3 →
  追加 `"flag":"专家长期同向，可能未独立分析"`

> 关于 `stance_diversity` 的谨慎说明：市场单边行情下专家**理应**同向，
> 低多样性本身不是错。故该指标**只作观测**，仅在样本 ≥10 且持续偏低时才提示。

**返回**：
```json
{"check":"output_diversity","status":"ok","reports_scanned":3,
 "summary_similarity":0.42,"drivers_overlap":0.15,
 "stance_diversity_avg":0.75}
```

**已知边界**：`difflib` 是**字符级**相似度，对**同义改写**不敏感——
若 LLM 换套说法表达同一个模板，相似度会偏低（**漏报**）。
彻底解决需语义相似度（可用 bge-m3），但成本更高，**本版先用 `difflib`**。

---

# 阶段 A —— 纵轴检验（现在可跑，但样本量无统计意义）

## A1 `check_reference_traceability(db, limit=90)` → dict

**目的**：检测首席是否编造新闻引用（幻觉引用）。

**匹配范围** ⭐**（修正）**：
- **对全表 `news` 匹配，不限日期。**
- 理由：`graph.py::_fetch_news()` 在"近 1 天无新闻"时会**回退到"取最新 N 条、不限日期"**。
  实测中 9/14 的报告引用的是 9/6 的新闻（相距 8 天）——若沿用初版的 `[date-1, date]` 窗口，
  这些**真实引用会全部匹配失败**，直接误报"疑似幻觉引用"。
- **同时**：命中的新闻若 `publish_time < report.date - STALE_DAYS`（默认 7），
  记入 `stale_refs` **单独统计**，不计入 untraceable。

**归一化**：去空白；全角/半角标点统一；去除 `【】[]（）()《》` 等括号及其内容。

**命中判定**（满足任一）：
- 归一化(`news.title`) 包含 归一化(`t`)[:12]，或反之
- `difflib.SequenceMatcher` 相似度 ≥ 0.6 ⭐**（新增）**——避免把**合理转述**误判为编造

**边界**：`reference_news` 缺失 / 非数组 / 为空 → `status="skipped"` + reason。

**返回**：
```json
{"check":"reference_traceability","status":"ok","reports_scanned":3,
 "total_refs":10,"traceable":9,"hit_rate":0.9,
 "stale_refs":[{"date":"...","title":"...","news_date":"..."}],
 "untraceable":[{"date":"...","title":"..."}]}
```

**判定**：`hit_rate < 0.8` → 追加 `"flag":"疑似幻觉引用"`。

---

## A2 `check_fact_consistency(db, limit=90)` → dict

**目的**：检测首席在 `market_summary` 中编造数字。

**数字提取**：
- 涨跌幅型：`(-?\d+(?:\.\d+)?)\s*%`
- 点位型：`(\d{3,5}(?:\.\d+)?)\s*点`

**排除项**：`(?:19|20)\d{2}\s*年`（年份）、`第\s*\d+`（序数）、既无 `%` 又无"点"的整数。

**标的解析** ⭐**（新增）**：
- 取数字前后各 10 字为上下文，尝试解析主语：
  `上证`→`sh000001`｜`深证|成指`→`sz399001`｜`创业板`→`sz399006`｜
  `沪深300`→`sh000300`｜`标普`→`.INX`｜`纳斯达克`→`.IXIC`｜`道琼斯`→`.DJI`
- **解析成功** → **只与该标的比对**（消除"7 次机会"的宽松问题）
- **解析失败** → 与全部标的比对，命中即算，但标记 `"subject_unspecified": true`

**容差**：
- 涨跌幅 ±0.15 个百分点；数字附近 5 字内含「约/近/左右/超过/逾/超」→ 放宽至 ±0.5
- 点位 ±2.0
- 同日 `market_data` 取 `date <= report.date` 的最近一条

**判定**：`match_rate < 0.9` → 追加 `"flag":"存在编造数字风险"`。

> 已知覆盖局限：点位型正则要求 `\s*点`，而当前综述写作「至3888.111」（无"点"字），
> 故点位检查基本不触发。**不为此放宽正则**（放宽会引入误判）。

---

## A3 `check_risk_level_distribution(db, limit=180)` → dict ⭐**（新增）**

**目的**：捕捉**当前唯一可观测的退化信号**——风险官是否退化为"永远判高"。

**背景**：若风险官每次都判"高"，则 `risk_veto` 常态化触发、置信度被永久压低，
该机制退化为噪声。实测首份报告即为 `risk_level="高"`、10 条风险点——**需警惕**。

**计算**：统计 `risk_level ∈ {高, 中, 低}` 的分布与占比，`high_ratio` = 高的占比。

**判定**：
- 样本 < 5 → `status="insufficient_data"`（仍返回当前分布供观察）
- 样本 ≥ 5 且 `high_ratio > 0.8` → 追加 `"flag":"风险官可能过度谨慎，降档机制趋于常态化"`

---

# 阶段 Q —— 数据质量检查（独立于 agent 评估）

## Q1 `check_category_coverage(db)` → dict

**认领关系**（依据 `EXPERTS_DESIGN.md` §4）：
宏观 = `宏观`+`政策`｜行业 = `行业`+`公司`｜资金面 = `资金`+`市场`｜技术面 = 不读新闻

**输出**：各类别条数、占比、是否被认领、未被认领的条数与占比。

**归一化漏网检测**：`data_agent.py::CATEGORY_ALIAS` 中作为 **key** 的词，
不应出现在 `news.category` 中。当前 `策略` 存在 1 条 → **应报出**
（系标签归一化修复前的残留）。

### Q2 `check_retrieval_health()` → dict

**目的**：检查两个「静默降级」部件的可用性——它们失效后系统照常运行，只是质量下降且无感知。

| 部件 | 失效表现 |
|------|---------|
| BM25 索引 | 为空 → RAG 退化为**纯向量检索**，关键词精确匹配能力缺失 |
| 交易日历 | 降级 → 按「工作日」猜测，**节假日可能被误判为交易日** |

**返回**：
```json
{"check":"retrieval_health","status":"ok",
 "bm25_ready":true,"bm25_size":350,"calendar_degraded":false}
```

**判定**：任一不可用 → 追加 `"flag"`。

> 注意：本检查调用 `is_degraded()`（只读），**不触发日历加载/联网/写盘**，以维持「评估层只读」约束。

---

# 阶段 B —— 骨架先写，等数据量

> 初版称"等字段迁移"。**更正**：字段已存在。真实门槛是**样本量**。
> 进入时仍用 `sqlalchemy.inspect` 检查 `report.risk_veto` 是否存在，
> 不存在则 `status="skipped"`（保留该防御，但预期不会触发）。

## B1 `check_risk_officer_calibration(db, limit=180)` → dict

- 按 `risk_veto` 分 True / False 两组
- 每组统计：n、次日 `|change_pct|` 均值、次日振幅均值 `(high-low)/前收`、次日下跌比例
- 两组 n 均 ≥ 20 时做 **Mann-Whitney U**（`scipy.stats.mannwhitneyu`），输出 `p_value`
- **verdict**：
  - `p_value < 0.05` 且 True 组更差 → `"风险官有预测力"`
  - 否则 → `"veto 可能为噪声，系统在无理由地降低置信度"`
  - 任一组 n < 20 → `status="insufficient_data"`

> ⚠️ 等待期提示：`risk_veto=True` 属**稀有事件**（仅风险官判"高"时触发），
> 累积到 n≥20 可能需**数月**。

## B2 `check_risk_officer_overcaution(db, limit=180)` → dict

- 筛选 `risk_veto=True` 且 `sentiment="偏多"` 的报告（风险官否决了一次看多）
- 计算其次日上涨比例 `up_ratio`
- `up_ratio > 0.55` → 追加 `"flag":"可能系统性误伤看多判断"`

## B3 `check_multi_vs_single_baseline` → dict ⭐**（新增，可选）**

**目的**：直接回答"多专家到底比单专家强吗"。

**方法**：每日同时跑「多专家版」与「单专家版」（`_fallback_report` 已有单次生成逻辑），
用同一批数据，两份都入库（或仅记录分数），累积后用 `compute_backtest` 的口径对比准确率。

**成本**：每天多 1 次 LLM 调用（单专家版很便宜）。

**状态**：**本次仅留设计，不实现**。需先确认是否愿意承担双份存储/双份调用。

---

# 阶段 C —— 不实现，仅留 TODO

`check_attribution_consistency`：将 `key_drivers` 声称的板块映射到板块行情，
检验次日是否真跑赢。

**数据源已就绪**（`sector_data` 表，自 2026-09-16 起采集），但历史累积极少，
尚不足以做归因统计，故仍不实现。**在函数体内留 TODO 注释，不要构造数据。**

---

## 9. 输出与集成规范

### 9.1 函数契约

1. 每个函数统一返回 dict，必须含 `check` / `status` 两键；
   `status ∈ {ok, skipped, insufficient_data, error}`；`skipped` 或 `error` 必须含 `reason`
2. **任何异常都不得向外抛出**：内部 `try/except` 捕获后返回 `status="error"` + `reason`

### 9.2 聚合与 API

3. `run_all_checks(db, limit=90, include_diagnostic=False) -> dict`
   返回 `{"generated_at":..., "checks":{...}}`
4. 新增 API：`app/api/routes_reports.py` 增加
   **`GET /api/v1/reports/evaluation`**（修正路径），调用 `run_all_checks`

### 9.3 诊断项与每日巡检的分离 ⭐**（新增）**

S1（敏感性）与 S2（复现性）会**额外产生 LLM 调用**（S1 约 3 次，S2 约 `runs` 次），
且其结果**不会逐日变化**（除非改了 prompt 或模型）。因此：

- `run_all_checks` **默认不跑 S1/S2**（`include_diagnostic=False`）
- 需显式传 `include_diagnostic=True`，或提供独立的
  `GET /api/v1/reports/evaluation?diagnostic=true`
- **建议触发时机**：改动 prompt / 换模型 / 调 temperature 之后手动跑一次

### 9.4 增量基线日志

5. `append_eval_log(db, path="data/eval_log.jsonl") -> dict`
   - 把 `run_all_checks` 结果以**追加**方式写入 JSONL（每行一条，含 `generated_at`）
   - **不得覆盖或截断**该文件
   - 在 `run_daily_pipeline()` 末尾调用
   - ⚠️ 说明：这会让 `run_daily_pipeline()` 产生**写文件副作用**（未违反约束 4，
     因其对外签名不变）。如需保持该函数"纯业务"，可改由调度器单独调用——
     **本版按原设计保留**，仅在文档中显式记录该副作用。

   > 用追加式 JSONL 而非一次性快照的理由：当前仅 3 份报告，一次性基线无统计意义。
   > 需持续累积基线，才能在多专家版本运行一段时间后做前后对比。

---

## 10. 验收标准

1. `compute_backtest()` 输出与本任务执行前**逐字段一致**
2. 所有新函数在空库 / 缺字段情况下**不抛异常**，返回 `skipped` 或 `insufficient_data`
3. 用当前 `data/app.db`（3 份报告）实际运行 `run_all_checks`，给出完整 JSON
4. **单独运行 S1 与 S2**（需 LLM 调用），给出结果
5. 连续调用两次 `append_eval_log`，`data/eval_log.jsonl` 应有 **2 行**（验证追加语义）
6. 明确列出哪些检验因数据未就绪而跳过，以及各自缺什么

---

## 11. 交付物

1. 改动清单：文件路径 + 新增函数名 + 行数
2. 一次实际运行的 `run_all_checks` 完整输出
3. **S1/S2 的独立运行结果**
4. 被跳过检验的清单及原因
5. `data/eval_log.jsonl` 的路径与首行内容
6. 一句话说明：在现有 3 份报告的样本量下，哪些结论**不能**得出

---

## 12. 本版已知的局限（诚实声明）

| 局限 | 说明 |
|------|------|
| **样本量** | n=3。A1/A2 的比率、B1/B2 的检验**均无统计意义**，只能验证"能跑通" |
| **A1 是近似** | 流水线未持久化"喂给 LLM 的新闻集合"，全表匹配是**近似**，非精确溯源 |
| **A2 覆盖有限** | 点位型数字因综述无"点"字而基本不触发 |
| **S1 用合成数据** | 极端输入能测"是否响应"，但**不能**测"响应幅度是否合理" |
| **S2 成本** | 每次 `runs` 次 LLM 调用，故默认不进每日巡检 |
| **S6 依赖近似语料** | 风险官原始输入未持久化，参考语料是**近似重建**；且合法风险可能不源于新闻 |
| **S7 对同义改写不敏感** | `difflib` 是字符级相似度，换套说法会**漏报**模板化 |
| **B 的等待期** | `risk_veto=True` 稀有，累积到 n≥20 可能需数月 |
| **只看形式，不看因果** | 评估的是"产出是否自洽/可溯/稳定"，**不评估"预测是否准确"**——后者是 `compute_backtest` 的职责 |
