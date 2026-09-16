# 金融新闻驱动的股市预测 Agent —— 系统设计文档

> 版本：v1.0
> 日期：2026-09-06
> 定位：每日自动采集金融新闻 + 行情数据，通过 RAG 增强的 LangChain Agent 生成股市走势研判与投资参考

---

## 0. 免责声明（必须放在最前面）

- 本系统产出的所有「预测」「研判」「信号」均为**基于历史数据与新闻语义的统计性参考**，**不构成任何投资建议**。
- 股市受政策、突发事件、市场情绪、流动性等多因素影响，模型无法穷举，**预测必然存在误差**。
- 系统所有输出必须标注「仅供参考，不构成投资建议」。
- 不要接入自动下单/自动交易，本系统定位是「信息收集 + 研判辅助」，不是「量化交易系统」。

---

## 1. 项目概述

### 1.1 目标

构建一个**每日自动运行**的智能 Agent，做到：

1. **每天定时采集**：金融新闻（中文为主，兼顾英文）、股票行情（指数/个股/K 线/成交量/资金流）。
2. **构建知识库**：将新闻向量化存入 ChromaDB，用 bge-m3 做多语言嵌入与混合检索。
3. **智能研判**：LangChain Agent 结合 RAG 检索到的新闻 + 实时行情数据，调用工具生成「市场研判 / 板块机会 / 个股提示 / 风险提示」。
4. **交互呈现**：Streamlit 提供看板（每日研判、新闻流、历史预测、问答交互）。

### 1.2 核心能力清单

| 能力 | 说明 |
|------|------|
| 自动采集 | 定时抓取新闻 + 行情，无需人工干预 |
| 语义检索 | 基于 bge-m3 的中英混合检索，支持「今天有什么利好」这类自然语言查询 |
| 多源融合 | 新闻（非结构化）+ 行情（结构化）在 Agent 内统一推理 |
| 每日研判 | 每日生成一份结构化的市场研判报告并入库留档 |
| 可追溯 | 每条结论附「依据新闻 + 数据来源」，可回查 |
| 人机交互 | Streamlit 聊天式问答 + 可视化看板 |

### 1.3 非目标（v1 不做）

- ❌ 自动交易 / 下单 / 实盘对接
- ❌ 高频/盘中实时预测（v1 只做日频）
- ❌ 复杂因子挖掘 / 深度强化学习选股
- ❌ 多用户权限体系

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      Streamlit 前端 (交互层)                      │
│   每日研判看板 │ 新闻流 │ 行情图表 │ 问答聊天 │ 历史回查            │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP / WebSocket
┌──────────────────────────────▼──────────────────────────────────┐
│                        FastAPI 后端 (服务层)                      │
│   REST API │ 任务调度(APScheduler) │ Agent 服务 │ 数据服务        │
└───────┬──────────────────┬──────────────────┬───────────────────┘
        │                  │                  │
┌───────▼───────┐  ┌───────▼────────┐  ┌──────▼──────────────┐
│ 数据采集层      │  │ Agent 决策层     │  │ RAG 知识层            │
│ 新闻爬虫/API   │  │ LangChain Agent │  │ ChromaDB (向量库)     │
│ 行情 API       │  │ Tools 工具集    │  │ bge-m3 (嵌入模型)     │
│ 数据清洗/入库  │  │ Prompt 模板     │  │ 检索器(混合检索)      │
└───────┬───────┘  └───────┬────────┘  └──────┬───────────────┘
        │                  │                  │
        └──────────────────▼──────────────────┘
                 ┌────────────────────────────┐
                 │   存储层                    │
                 │  PostgreSQL/SQLite (结构化) │
                 │  ChromaDB 持久化目录        │
                 │  日志 / 报告归档            │
                 └────────────────────────────┘
```

### 2.1 分层职责

| 层 | 职责 | 关键组件 |
|----|------|----------|
| 交互层 | 展示、问答、参数配置 | Streamlit |
| 服务层 | 对外 API、调度、编排各层 | FastAPI + APScheduler |
| 采集层 | 定时抓取、清洗、落库 | AkShare / yfinance / RSS / 爬虫 |
| Agent 层 | 决策推理、工具调用、研判生成 | LangChain + LLM |
| 知识层 | 向量化、检索 | ChromaDB + bge-m3 |
| 存储层 | 结构化数据、向量、归档 | PostgreSQL / SQLite + Chroma |

---

## 3. 技术栈选型

### 3.1 已确定（用户指定）

| 组件 | 版本/选型 | 备注 |
|------|-----------|------|
| Python | 3.11 | |
| 后端框架 | FastAPI + Uvicorn | 异步、自动生成 OpenAPI 文档 |
| Agent 框架 | LangChain | 用最新 `langchain` + `langchain-community` + `langchain-huggingface` |
| 前端 | Streamlit | 快速原型，无需写 HTML/CSS |
| 向量库 | ChromaDB | 本地持久化，零运维，`chromadb` |
| 嵌入模型 | bge-m3 (BAAI/bge-m3) | 多语言、混合检索（dense + sparse + colbert） |

### 3.2 建议补充的选型

| 用途 | 推荐 | 备选 | 说明 |
|------|------|------|------|
| LLM（研判推理） | 按你已有 API：DeepSeek / Qwen / GPT / Claude | — | 建议用支持 function calling 的模型 |
| 任务调度 | **APScheduler** | Celery + Redis | v1 用 APScheduler 足够（进程内定时），省 Redis |
| 结构化数据库 | **SQLite（起步）→ PostgreSQL（上线）** | — | SQLAlchemy ORM 统一，切换只改连接串 |
| 中文行情数据 | **AkShare**（免费） | Tushare Pro（需 token） | AkShare 覆盖 A 股/指数/资金流/板块 |
| 国际行情 | **yfinance** | Alpha Vantage | 美股、汇率、商品 |
| 新闻源 | RSS + 财经 API + 定向爬虫 | NewsAPI / GNews | 见 4.1 |
| 嵌入推理 | `FlagEmbedding` 库 或 `sentence-transformers` | — | bge-m3 官方支持两种加载方式 |
| HTTP 客户端 | httpx / aiohttp | requests | 采集层用异步 |
| 配置管理 | pydantic-settings | python-dotenv | 环境变量 + `.env` |
| 日志 | loguru | logging | 结构化日志 |

### 3.3 bge-m3 关键说明（重要）

- **全称**：BAAI General Embedding，Multi-lingual / Multi-functionality / Multi-granularity。
- **多语言**：支持 100+ 语言，中英混排无压力。
- **多向量输出**：
  - **dense**（稠密向量，1024 维）：语义匹配主力。
  - **sparse**（稀疏词权重）：关键词精确匹配，适合「GDP」「降准」这类术语。
  - **colbert**（多向量）：细粒度匹配，效果好但存储大、检索慢，**v1 建议只用 dense + sparse**。
- **检索策略**：dense + sparse 混合（加权融合，如 0.7*稠密 + 0.3*稀疏），对金融术语召回率显著优于纯 dense。
- **落地建议**：
  - 用 `FlagEmbedding` 包加载 `BAAI/bge-m3`，CPU 可跑但慢，**建议有 GPU 或至少 8GB 内存**。
  - 若本地资源有限，可先跑 `BAAI/bge-small-zh-v1.5` 或 `bge-large-zh-v1.5` 过渡，接口统一后无缝切 bge-m3。
  - 向量维度 1024，注意 ChromaDB collection 创建后维度不可改。

---

## 4. 数据采集层

### 4.1 新闻源（中文为主）

| 源 | 方式 | 说明 |
|----|------|------|
| 新浪财经 / 东方财富 | 公开 API + RSS | 实时快讯、公司公告 |
| 财联社电报 | 爬虫（需注意反爬/合规） | 快讯时效性高 |
| 华尔街见闻 | RSS | 宏观 + 全球市场 |
| 证券时报 / 上证报 | RSS | 政策面权威 |
| Reuters / Bloomberg | RSS（免费部分） | 英文，bge-m3 直接处理 |
| 央行 / 证监会 / 统计局 | 官网公告抓取 | 政策数据（LPR、CPI、PMI 等） |

> 合规提醒：爬虫务必遵守 robots.txt、控制频率、注明来源、仅用于个人研究；商业用途需申请授权。

### 4.2 行情数据源

| 数据 | 来源 | 说明 |
|------|------|------|
| A 股指数（上证/深证/创业板/沪深300） | AkShare | 日线、分钟线 |
| 个股行情/财务 | AkShare | 股价、PE、市值、涨跌幅 |
| 资金流向/北向资金 | AkShare | 情绪面关键指标 |
| 板块/行业数据 | AkShare | 板块轮动 |
| 美股指数/个股 | yfinance | 道指/纳指/标普 |
| 汇率/商品 | yfinance / AkShare | 美元指数、原油、黄金 |

### 4.3 数据模型（核心表结构）

```
news 新闻表
  id            UUID PK
  title         TEXT         标题
  content       TEXT         正文
  source        TEXT         来源
  url           TEXT UNIQUE  原文链接（去重键）
  publish_time  DATETIME     发布时间
  category      TEXT         分类(宏观/行业/公司/政策/国际)
  sentiment     FLOAT        情绪分（可选，Agent 或规则打）
  summary       TEXT         摘要（LLM 生成）
  collected_at  DATETIME     采集时间

market_data 行情表
  id            UUID PK
  symbol        TEXT         代码(000001.SH / AAPL)
  name          TEXT         名称
  date          DATE         交易日
  open/high/low/close/volume  OHLCV
  change_pct    FLOAT        涨跌幅
  turnover      FLOAT        成交额

macro_data 宏观数据表
  id / name / value / publish_date / source / freq

sector_data 行业板块表（2026-09-16 新增）
  id / date / name / change_pct / avg_price / volume / turnover
  / company_count / leader / source
  唯一约束 (date, name) —— 即"去重"，结构化数据无需语义判重

report 研判报告表
  id            UUID PK
  date          DATETIME     报告时间（唯一）
  report_type   TEXT         daily(日报) / weekly(周报)
  title         TEXT         标题
  content       TEXT         完整报告（JSON）
  sentiment     TEXT         偏多/中性/偏空
  confidence    TEXT         high/medium/low
  score         FLOAT        综合情绪分 -1~1
  expert_opinions TEXT       JSON：5 位专家原始观点
  divergence    FLOAT        专家分歧度 0~2
  risk_veto     BOOLEAN      风险官是否触发置信度降档
  low_info      BOOLEAN      当日四信号均未触发（市场平静）
  data_stale    BOOLEAN      近期新闻不足、基于陈旧数据生成
  model         TEXT         模型名
  created_at    DATETIME
```

> 注：本文档为 v1.0 原始设计。后续演进见 `AGENTS_DESIGN.md`（多 Agent）、
> `EXPERTS_DESIGN.md`（多专家分析）、`EVAL_SPEC.md`（评估层）。

### 4.4 采集调度

- 用 APScheduler 配置 **cron 任务**：
  - 收盘后行情采集：工作日 `17:30`（A 股收盘 15:00 后数据完整）。
  - 新闻增量采集：每 `30 分钟` 增量抓一次，或每日 `17:00` 批量抓当天。
  - 板块数据采集：`17:40`（行业板块日频快照，走本地日缓存避免限流）。
  - 每日研判生成：`18:00`（等行情 + 新闻都齐了）。
  - 周度综述生成：`18:30`，**仅在本周最后一个交易日**实际执行。
  - **交易日守卫**：行情采集 / 每日研判 / 周报均先查 A 股交易日历
    （`app/collectors/trading_calendar.py`），非交易日自动跳过；
    新闻采集为 7×24，不受影响。
- 去重：新闻以 `url` 或 `title+publish_time` 做唯一约束；行情以 `symbol+date` 唯一。
- 失败重试：统一由 `app/retry.py` 提供（指数退避，3 次），**覆盖采集与全部 LLM 调用**；
  按 HTTP 状态码/异常类型判定是否值得重试（不匹配错误消息文本）。

---

## 5. RAG 知识层

### 5.1 构建流程（离线索引）

```
新闻入库 → 清洗/切分(chunk) → bge-m3 嵌入 → 写入 ChromaDB
         ↓
   保留 metadata: {news_id, title, source, publish_time, category}
```

1. **切分（chunking）**：金融新闻通常短，一条新闻 = 1~2 个 chunk（`RecursiveCharacterTextSplitter`, chunk_size≈500, overlap≈50）。
2. **嵌入**：`FlagEmbeddingModel.encode()` 得到 dense + sparse 两套向量。
3. **存储**：
   - dense 向量 → ChromaDB（collection 维度 1024，`HNSW` 索引）。
   - sparse 向量 → ChromaDB 也支持（`chromadb` 的 sparse 向量），或自建 BM25 检索器放在内存/文件。
   - 原始文本 + 结构化字段 → 结构化库，Chroma 里只存 `news_id` 关联。

### 5.2 检索策略（混合检索 + 重排）

```
用户查询
  ├─ dense 检索 (语义，ChromaDB cosine)  → top_k 候选
  ├─ sparse 检索 (关键词，BM25/sparse)   → top_k 候选
  └─ 融合：RRF (Reciprocal Rank Fusion) 或加权分数
        ↓
    精排：bge-reranker-v2-m3 (cross-encoder) 对融合结果打分 → 取 top 5~10 作为上下文
```

- 推荐 **RRF 融合**，实现简单且稳定，避免分数尺度不一致问题。
- 检索时按 `publish_time` 过滤「近 N 天」新闻，保证时效性。
- 每个 chunk 的 metadata 带 `news_id`，检索到后回查结构化库拿完整原文。

### 5.3 检索器对外接口

```python
class NewsRetriever:
    def hybrid_search(query: str, top_k=10, days=7) -> list[Document]
    def search_by_keyword(keyword: str, days=30) -> list[Document]
    def search_similar(news_id: str, top_k=5) -> list[Document]
```

---

## 6. Agent 决策层（LangChain）

### 6.1 Agent 形态选择

| 形态 | 说明 | 推荐度 |
|------|------|--------|
| **ReAct Agent（function calling）** | LLM 自主决定调哪些工具，灵活 | ⭐⭐⭐ v1 主推 |
| LangGraph 有状态多步流程 | 固定 pipeline（采集→检索→分析→成文），可控可测试 | ⭐⭐⭐ 每日研判用这个 |
| 多 Agent 协作 | 宏观/行业/个股各一个子 Agent | v2 再做 |

**建议双形态**：
- **每日定时研判** → 用 **LangGraph 固定流程**（可控、可复现、输出稳定）。
- **交互式问答** → 用 **ReAct Agent**（灵活，用户问什么查什么）。

### 6.2 Tools 工具集（Agent 可调用）

| 工具 | 功能 | 底层 |
|------|------|------|
| `search_news` | 语义检索相关新闻 | NewsRetriever.hybrid_search |
| `get_market_data` | 查指数/个股行情 | 行情服务（读库或实时 API） |
| `get_fund_flow` | 查资金流向 | AkShare |
| `get_macro_data` | 查宏观数据（CPI/PMI/LPR） | 宏观表 / AkShare |
| `get_sector_data` | 查板块涨跌 | AkShare |
| `web_search`（可选） | 联网搜最新消息 | Tavily / Bing |
| `get_report_history` | 查历史研判报告 | 报告表 |

每个工具都用 LangChain 的 `@tool` 装饰器 + 清晰的 docstring（LLM 靠它判断何时调用）。

### 6.3 研判生成流程（LangGraph 固定流程）

```
① 输入：日期 D
② 采集检查：D 的新闻、行情是否齐全（缺失则补采）
③ 检索：按「市场/政策/资金/行业/国际」多路检索当天新闻
④ 数据组装：指数行情 + 资金流 + 宏观日历 + 检索新闻
⑤ LLM 分析：分维度分析（宏观面/资金面/情绪面/技术面）
⑥ 生成报告：结构化 JSON（结论 + 依据 + 置信度 + 风险）
⑦ 校验入库：写入 report 表 + 打分存档
```

### 6.4 Prompt 设计要点

- **System Prompt** 明确角色：资深宏观 + 行业分析师。
- **强制结构化输出**：让 LLM 输出固定 JSON（用 `with_structured_output` 或 pydantic 校验）。
- **要求标注依据**：每条结论必须引用「来源新闻标题 + 数据点」。
- **要求给置信度**：`high / medium / low`，并说明不确定性来源。
- **免责声明**：输出末尾固定追加风险提示。

结构化输出示例（pydantic 模型）：

```python
class MarketReport(BaseModel):
    market_summary: str            # 大盘综述
    sentiment: Literal["偏多","中性","偏空"]
    confidence: Literal["high","medium","low"]
    key_drivers: list[str]         # 主要驱动因素（附新闻依据）
    sector_opportunities: list[str] # 板块机会
    risks: list[str]               # 风险提示
    reference_news: list[str]      # 引用新闻 ID
    disclaimer: str                # 免责声明
```

### 6.5 LLM 选型建议

- 需要支持 **function calling / 工具调用** + **结构化输出**。
- 中文效果好、成本可控的：DeepSeek-V3、Qwen-Max、GPT-4o/Claude 系列均可。
- 通过环境变量配置，模型可切换（LangChain 的 `ChatOpenAI` 兼容多家 API 端点）。

---

## 7. 后端服务层（FastAPI）

### 7.1 API 设计

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/v1/reports/today` | 今日研判报告 |
| GET | `/api/v1/reports?date=YYYY-MM-DD` | 历史报告 |
| GET | `/api/v1/news?date=&keyword=&limit=` | 新闻查询 |
| GET | `/api/v1/market?symbol=&start=&end=` | 行情查询 |
| POST | `/api/v1/agent/chat` | 交互问答（调用 Agent） |
| POST | `/api/v1/agent/generate` | 手动触发研判生成 |
| GET | `/api/v1/health` | 健康检查 |

### 7.2 模块划分

```
app/
  main.py              # FastAPI 入口，挂路由 + 启动调度器
  config.py            # pydantic-settings 配置
  api/                 # 路由层
    routes_reports.py
    routes_news.py
    routes_market.py
    routes_agent.py
  services/            # 业务层
    news_service.py
    market_service.py
    report_service.py
    agent_service.py
  collectors/          # 采集层
    news_collector.py
    market_collector.py
    scheduler.py       # APScheduler 任务注册
  rag/                 # 知识层
    embeddings.py      # bge-m3 封装
    vector_store.py    # ChromaDB 封装
    retriever.py       # 混合检索
  agent/               # 决策层
    tools.py           # 工具定义
    prompts.py         # Prompt
    graph.py           # LangGraph 研判流程
    react_agent.py     # 交互式 Agent
  models/              # SQLAlchemy ORM
  db.py                # 数据库连接
```

### 7.3 关键集成点

- **启动时**：初始化 bge-m3 模型（加载一次，全局单例）、连数据库、初始化 ChromaDB、启动 APScheduler。
- **bge-m3 单例**：模型加载慢（数 GB），务必全局只加载一次，避免每个请求重复加载。

---

## 8. 前端交互层（Streamlit）

### 8.1 页面设计

| 页面 | 内容 |
|------|------|
| 首页/看板 | 今日研判报告、市场情绪仪表盘、指数涨跌卡 |
| 新闻流 | 按日期/关键词/分类筛选的新闻列表（可点开详情） |
| 行情 | 指数/个股 K 线图、资金流图（plotly） |
| 问答 | 聊天窗口，调用 `/agent/chat`，支持流式输出 |
| 历史 | 历史报告列表、预测准确率回测 |
| 设置 | API 地址、模型选择、采集频率配置 |

### 8.2 交互实现

- Streamlit 用 `requests`/`httpx` 调 FastAPI（前后端解耦，Streamlit 可独立跑）。
- 聊天用 `st.chat_message` + `st.chat_input`。
- 图表用 **plotly**（交互好，暗色主题适配）。
- 预测历史准确率用简单规则回测（方向对不对），v1 仅供参考。

---

## 9. 关键流程时序（每日主流程）

```
17:30  Scheduler 触发 → 采集行情 → 写入 market_data
18:00  Scheduler 触发 → 采集新闻 → 写入 news → 增量索引到 ChromaDB
18:15  生成研判 → LangGraph 流程 → 检索+分析 → 报告入库 report
次日   用户打开 Streamlit → 拉取今日报告 → 查看/追问 → Agent 回答
```

---

## 10. 项目目录结构（完整）

```
WelcomeScreen/
├── DESIGN.md                  # 本文档
├── README.md
├── .env.example               # 环境变量样例
├── requirements.txt
├── pyproject.toml
├── app/
│   ├── main.py
│   ├── config.py
│   ├── db.py
│   ├── api/
│   ├── services/
│   ├── collectors/
│   ├── rag/
│   ├── agent/
│   └── models/
├── frontend/
│   └── streamlit_app.py       # 或 app.py
├── data/                      # ChromaDB 持久化 + SQLite 文件
├── logs/
└── tests/
```

---

## 11. 依赖清单（requirements 核心）

```
fastapi
uvicorn[standard]
langchain
langchain-community
langchain-huggingface
langgraph
chromadb
FlagEmbedding          # bge-m3 加载
sentence-transformers  # 备选加载方式
akshare
yfinance
pandas
sqlalchemy
apscheduler
httpx
loguru
pydantic
pydantic-settings
streamlit
plotly
tenacity
python-dotenv
```

---

## 12. 实施路线图（分阶段，适合 vibecoding）

### Phase 1 —— 跑通最小闭环（1~2 天）
1. 环境搭建：Python 3.11 + venv + 装依赖。
2. 写 `config.py` + `.env`。
3. 打通行情采集（AkShare 拉上证指数）。
4. 打通新闻采集（一个 RSS 源即可）。
5. 打通 bge-m3 嵌入 + ChromaDB 存/查。
6. 一个最简单的 ReAct Agent 能问答。

### Phase 2 —— RAG 打通（1 天）
1. 新闻入库 → 切分 → 嵌入 → 索引。
2. 混合检索器（dense + sparse + RRF）。
3. Agent 接入 `search_news` 工具，验证「根据今天新闻分析大盘」。

### Phase 3 —— 每日研判自动化（1~2 天）
1. LangGraph 固定研判流程 + 结构化输出。
2. APScheduler 定时任务。
3. 报告入库 + 历史查询 API。

### Phase 4 —— 前端（1 天）
1. Streamlit 看板 + 新闻流 + 问答聊天。
2. 图表可视化。

### Phase 5 —— 打磨（可选）
1. 预测回测 / 准确率统计。
2. 情绪打分（可选微调小模型或 LLM 打分）。
3. 多源新闻扩充、异常告警。
4. 迁移 PostgreSQL、容器化部署（Docker）。

---

## 13. 风险与注意事项

| 风险 | 应对 |
|------|------|
| **LLM 幻觉** | 强制引用来源、要求标注置信度、结构化输出校验 |
| **预测不准** | 明确免责声明，加回测模块暴露真实准确率 |
| **爬虫被封/合规** | 控制频率、遵守 robots、保留来源、优先官方 API |
| **bge-m3 资源占用** | 本地无 GPU 时降级 bge-small/large，或云端嵌入 API |
| **数据缺失/源失效** | 采集加 try/except + 重试 + 多源冗余 + 告警 |
| **Agent 工具误调用** | 工具 docstring 写清楚、输入 schema 用 pydantic 校验 |
| **成本** | LLM 按次计费，批量研判每日 1 次 + 缓存，避免无谓调用 |
| **时区/交易日** | A 股节假日日历（用 AkShare 交易日历），非交易日跳过 |

---

## 14. 已确认的技术决策（2026-09-06）

| 决策项 | 结论 |
|--------|------|
| LLM | **DeepSeek**（`deepseek-chat`，兼容 OpenAI API，走 `langchain-openai`） |
| 中文新闻源 | **新浪财经、东方财富、财联社** |
| 数据库 | **SQLite**（SQLAlchemy，后续可平滑迁移 PostgreSQL） |
| 嵌入/重排模型 | 本地 **bge-m3**（嵌入，dense+sparse）+ **bge-reranker-v2-m3**（精排） |
| 情绪打分 + 回测 | **v1 都要做**：新闻 LLM 情绪打分（-1~1）、预测方向回测 |

---

*本文档仅描述系统设计，所有预测输出不构成投资建议。*
