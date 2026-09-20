# 金融新闻驱动的股市预测 Agent

每天自动采集金融新闻（新浪财经 / 东方财富 / 财联社）与行情数据（A股 + 美股），通过
**bge-m3 + ChromaDB 混合检索（RAG）** 增强的 **LangChain Agent**，生成每日市场研判，
并提供 Streamlit 看板与问答交互。

> ⚠️ **免责声明**：所有研判/预测均为基于历史数据与新闻语义的统计性参考，**不构成投资建议**。

## 技术栈

- Python 3.11 · FastAPI · LangChain · Streamlit
- RAG：bge-m3（嵌入）+ bge-reranker-v2-m3（精排）+ ChromaDB + BM25 混合检索
- 数据：AkShare（A股）· yfinance（美股）· httpx（新闻）
- LLM：DeepSeek（OpenAI 兼容协议）
- 调度：APScheduler · 存储：SQLite

## 快速开始

### 1. 安装依赖

```bash
cd <项目根目录>
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

> bge-m3 / bge-reranker-v2-m3 体积较大，首次运行会从 HuggingFace 下载；
> 若本地已有模型，请把 `.env` 中的路径指向本地目录。

### 2. 配置环境变量

```bash
copy .env.example .env
```

编辑 `.env`，至少填入 `DEEPSEEK_API_KEY`。

### 3. 启动后端

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- 启动时会自动建表 + 启动定时调度器。
- API 文档：http://localhost:8000/docs

### 4. 启动前端

```bash
streamlit run frontend/streamlit_app.py
```

打开浏览器访问（默认 http://localhost:8501）。

## 手动触发（可选，不依赖定时任务）

```bash
# 采集新闻 + 索引
curl -X POST http://localhost:8000/api/v1/news/collect
# 采集行情
curl -X POST http://localhost:8000/api/v1/market/collect
# 生成每日研判
curl -X POST http://localhost:8000/api/v1/reports/generate
```

## 定时任务（默认已配置）

| 任务 | 时间 |
|------|------|
| 新闻增量采集 | 每 30 分钟 |
| 行情采集 | 工作日 17:30 |
| 板块数据采集 | 工作日 17:40 |
| 每日研判生成 | 工作日 18:00 |
| 周度综述生成 | 本周最后一个交易日 18:30 |

> **交易日守卫**：行情采集、每日研判、周报均先查 A 股交易日历，
> **非交易日自动跳过**（新闻采集 7×24 不受影响）。

## 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/reports/today` | 最近一份日报 |
| POST | `/api/v1/reports/generate` | 手动生成研判 |
| GET | `/api/v1/reports/weekly` | 最近一份周报 |
| POST | `/api/v1/reports/weekly/generate` | 手动生成周报 |
| GET | `/api/v1/reports/backtest` | 预测方向回测 |
| GET | `/api/v1/reports/evaluation` | 评估层（加 `?diagnostic=true` 跑敏感性/复现性） |
| GET | `/api/v1/news` | 新闻列表 |
| POST | `/api/v1/news/collect` | 采集新闻 |
| GET | `/api/v1/market` | 行情列表 |
| POST | `/api/v1/market/collect` | 采集行情 |
| GET | `/api/v1/sectors` | 板块涨跌排行（可选 `?target_date=YYYY-MM-DD`） |
| POST | `/api/v1/agent/chat` | AI 问答 |

## 目录结构

```
app/
  main.py            FastAPI 入口
  config.py          配置（.env）
  db.py              数据库连接
  models/            ORM 模型
  collectors/        采集（新闻/行情/调度）
  rag/               RAG（嵌入/向量库/混合检索/重排）
  agent/             决策（LLM/工具/研判/问答）
  services/          业务（入库/报告/回测）
  api/               路由
frontend/
  streamlit_app.py   前端看板
```

详见 `DESIGN.md`（完整设计文档）。

## 免责声明

本项目仅供学习与研究，输出不构成投资建议。股市有风险，投资需谨慎。
