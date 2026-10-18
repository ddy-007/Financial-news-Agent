# 金融新闻情报简报 Agent

每天自动采集金融新闻（新浪财经 / 东方财富 / 财联社）与行情数据（A股 + 美股），经
**bge-m3 + ChromaDB 混合检索（RAG）** 与**多专家独立分析**——宏观 / 行业 / 资金面 /
技术面四位分析师各自研判，风险官在其后逐条质疑，首席汇总——输出一份
**市场情报简报**：今天发生了什么、各维度怎么看、风险点在哪。

并提供 **Streamlit 看板**与**问答交互**。

> ## ⚠️ 这不是涨跌预测系统
>
> 本系统的产出是**情报整理与观点呈现**，**不承诺预测准确率**。
>
> 日频涨跌的信噪比极低，这也是为什么项目的回测模块（`/api/v1/reports/backtest`）
> 被定位成**检验判断质量的自我检查**，而不是用来说"它能预测未来"。
> 目前积累的样本量还很小（**个位数**），**任何"准确率"数字都还没有统计意义**。
>
> 之所以强调这一点：本项目在设计上反复坚持「结论强度不超过数据支持的强度」
> （见 `EVAL_SPEC.md` 对样本量的要求），对外表述也应当一致。
>
> 所有研判均为基于历史数据与新闻语义的统计性参考，**不构成投资建议**。

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
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- 启动时会自动建表 + 启动定时调度器。
- API 文档：http://localhost:8000/docs
- 就绪探针：http://localhost:8000/api/v1/health/ready （存活探针是 `/api/v1/health`）

> ⚠️ **默认只绑回环地址（127.0.0.1），这是 2026-09-25 改的，原来写的是 `0.0.0.0`。**
> 后端**没有任何鉴权**，且好几个接口是**写操作或会花钱的**：
> `POST /api/v1/news/collect`（触发采集 + 大量 LLM 分类）、`POST /api/v1/reports/generate`
> （整条多专家研判管线）、`POST /api/v1/agent/chat`（任意人打 LLM）。
> `DESIGN.md` §1.3 把「多用户权限体系」明确列为 **v1 非目标**，所以这不是代码缺陷 ——
> 但**一旦监听 `0.0.0.0` 且端口可被不受信任的网络访问，这些成本与数据就都是敞开的**。
>
> **要对外提供服务，请自己做**（本项目的定位是单机学习研究）：
> 1. 反向代理 + 鉴权（别让反代无保护转发）；
> 2. 用防火墙 / 安全组限制来源；
> 3. 收紧 `app/main.py` 里 `allow_origins=["*"]` 的 CORS 来源；
> 4. 不要因为「想远程看看」就改回 `0.0.0.0`。
>
> 另外：**不要用 `--workers N`**。每个 uvicorn worker 都会跑一遍 lifespan、
> 各起一个 `BackgroundScheduler`，于是采集与研判会**按 worker 数重复执行**
> （重复的 LLM 花费 + SQLite 写竞争）。`--reload` 没这个问题（父进程只监听文件、
> 不跑 lifespan，且重启是先停旧再起新）。

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
| 新闻增量采集 | 每 60 分钟（整点） |
| 行情采集 | 工作日 17:30 |
| 板块数据采集 | 工作日 17:40 |
| 每日研判生成 | 工作日 18:30 |
| 周度综述生成 | 本周最后一个交易日 19:30 |

> **交易日守卫**：行情采集、每日研判、周报均先查 A 股交易日历，
> **非交易日自动跳过**（新闻采集 7×24 不受影响）。

## 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/reports/today` | 最近一份日报 |
| POST | `/api/v1/reports/generate` | 手动生成研判 |
| GET | `/api/v1/reports/weekly` | 最近一份周报 |
| POST | `/api/v1/reports/weekly/generate` | 手动生成周报 |
| GET | `/api/v1/reports/backtest` | 方向回测（判断质量的自我检查，非战绩展示） |
| GET | `/api/v1/reports/evaluation` | 评估层（加 `?diagnostic=true` 跑敏感性/复现性） |
| GET | `/api/v1/news` | 新闻列表（可选 `days` / `start`+`end` / `keyword` / `limit`） |
| GET | `/api/v1/news/dates` | 有新闻的日期清单（含每天条数，供日期筛选用） |
| POST | `/api/v1/news/collect` | 采集新闻 |
| GET | `/api/v1/news/sources/health` | **采集源健康**（哪个源多久没成功、连续空轮、是否被截断）|
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

## 新闻原文恢复与区间补采

新闻在去重前保存完整原文，分类失败后可从本地队列重试；索引失败可直接重建索引，
不重复分类。抓取水位与处理水位分开，历史缺口不会因后续增量成功而消失。
健康接口额外返回每源 `pending_count` / `open_gap_count`；报告按取数时的窗口保存覆盖提示。
本轮内语义去重采用完整链接约束，每个自动合并组两两相似度均须达到 0.85。

先生成指定区间的本地预览（联网抓取、只读业务库，不调用模型）：

```powershell
.venv/Scripts/python.exe -B scripts/backfill_news.py --source 新浪财经 --start "2026-10-15 13:59:56" --end "2026-10-15 16:08:02" --max-pages 120 --max-seconds 120 --output data/backfill-preview.json
```

预览包含区间记录数、URL 未匹配候选数和模型请求次数保守上界。候选不等于真正缺失，
后续语义去重可能合并；费用按模型及 token 数另行估算。翻页有页数、间隔和时间预算，
时间预算在页与页之间检查，正在执行的一页请求及重试可能额外耗时。

确认费用与写库范围后，显式应用已保存预览：

```powershell
.venv/Scripts/python.exe -B scripts/backfill_news.py --apply data/backfill-preview.json --max-items 100
```

此命令可能调用付费模型、写新闻/原文/缺口表，并进行仅涉及新闻恢复的增量迁移。
`--max-items` 限制当前重新核对后的待处理量，超限先退出；它不是价格上限。
普通采集与补采共用跨进程互斥锁。缺口仅在扫描达到时间边界、无失败/拒绝条目且处理全部完成时关闭；
空页、游标停滞或页数上限不代表完整覆盖。达不到窗口时退出码为 2，已得数据仍可预览/处理，
缺口保持待补；无法保证上游仍保留历史数据。

CLI 更新共享的持久化向量索引并重建本进程 BM25。常驻后端的 BM25 是独立内存缓存，
补采后需重启/开发模式重载后端，或等待下一次会重建 BM25 的正常采集；仅查健康接口不会刷新缓存。

后端启动会自动增加 `raw_news`、`collection_gap` 表及可空抓取字段，保留旧业务数据；
旧库不自动重建历史原文或缺口。回退代码时可以保留新增表。原文暂不自动清理，会增加磁盘占用。
详细状态契约见 `AGENTS_DESIGN.md` §5.1；离线验证运行：

```powershell
.venv/Scripts/python.exe -B scripts/check_news_recovery_contracts.py
```

## 日报选取与问答时间范围

日报从分析时点前 24 小时的完整候选池选取新闻，默认按四段时间配额及重要性排序，
每类最多 20 条、风险官最多 60 条；综合类最多 10 条。来源数量重新按名称去重，仅用于弱排序，
不直接改变综合评分，也不代表独立信源印证。可通过 `.env.example` 中的 `NEWS_PER_CATEGORY`、
`NEWS_ALL_LIMIT`、`NEWS_SELECTION_SEGMENTS`、`NEWS_RECENCY_GRAVITY`、`NEWS_SOURCE_WEIGHT` 配置。

问答语义检索默认近 7 天；“今天”“昨天”及 `YYYY-MM-DD` 查询限定相应日期。
历史问题可由工具提供 start/end，显式日期优先。向量和关键词两路都先限制到相同数据库时间窗，
避免旧闻挤占近期候选；较大的历史范围会增加临时 BM25 的构建开销。日期未知的新闻保留空值，
不会冒充今日消息。专家、风险官、首席与周报输出格式错误也会获得备用模型机会；多个不同
JSON 候选会明确失败，不由程序猜选。前端主风险清单完整保留，风险官抽屉避免重复展示。

离线回归：`.venv/Scripts/python.exe -B scripts/check_document_bugfix_contracts.py`。

## 免责声明

本项目仅供学习与研究，输出不构成投资建议。股市有风险，投资需谨慎。
