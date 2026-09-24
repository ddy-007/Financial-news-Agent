"""数据采集 Agent（混合 Pipeline）。

流程（**2026-09-22 顺序重构**）：
    采集 → ①确定性去重（免费） → ②批量嵌入 + 语义近邻 → ③LLM 相关性分类 → ④入库 → ⑤索引

**为什么改顺序**：业界共识是「便宜且高选择性的步骤放前面，昂贵的放最后」
（mixpeek：*place a dedup stage as early as possible to minimize the number of
documents flowing through expensive downstream stages*）。我们原来恰好是反的 ——
「LLM 分类（最贵）→ 语义去重 → 合并」，于是**重叠窗口里上一轮刚抓过的条目，
也要重新过一次 LLM 分类**。实测稳态轮约 68% 的条目属于这种重复（22:30 那轮：
采集 220 → 相关 168，其中真正新增只有 18、合并 150）。

定位：LLM 只做语义判断（相关性、灰色复核），向量/代码做确定性计算（相似度、合并字段）。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from loguru import logger
from sqlalchemy.orm import Session

from app.agent.llm import get_llm, get_llm_model_name, llm_retry_times
from app.agent.prompts import CLASSIFY_PROMPT, DEDUP_PROMPT
from app.collectors.news_collector import NewsItem, collect_all_news
from app.models.news import News
from app.retry import call_with_retry, is_retryable
from app.rag import vector_store
from app.rag.embeddings import embed_documents
from app.rag.retriever import get_retriever

_WS = re.compile(r"\s+")


def _doc_text(title: str, content: str | None) -> str:
    """入库文本的**唯一**格式。

    嵌入与索引必须走同一个函数 —— 改动前 `_index_new` 用 `f"{title}\n{content}"`、
    `_find_similar` 用 `f"{title} {content}"`，分隔符不一致，是埋着的坑。
    """
    return f"{title}\n{content or ''}"

BATCH_SIZE = 20       # LLM 批量分类每批条数

# 连续多少批分类失败就**熔断**（中断本轮剩余批次）。
#
# 连续失败通常意味着 LLM **不可用**（连接断了 / 额度耗尽 / key 失效），
# 而不是"这一批运气不好"。此时把剩下的批次全打完只是白等 + 把日志刷爆：
# 2026-09-22 实测连接中断后 **0.43 秒内刷出 110 条报错**，55 批（1120 条）全废。
#
# 3 批足够区分「偶发抖动」（重试后多半就成功了）与「整体不可用」。
# 与采集层的 `MAX_CONSECUTIVE_PAGE_FAILS` 是同一个思路。
MAX_CONSECUTIVE_BATCH_FAILS = 3

# 批间**抖动**指数退避的上下界（秒，B1，2026-09-24）
BACKOFF_BASE = 1.0
BACKOFF_CAP = 8.0


def _backoff_delay(attempt: int, *, base: float = BACKOFF_BASE,
                   cap: float = BACKOFF_CAP) -> float:
    """第 `attempt` 次连续失败时该等多久 —— **抖动**指数退避。

    **为什么必须有**：2026-09-22 实测连接中断后 `0.433 秒内刷完 55 批`
    （3.9 毫秒/批）—— 循环里没有任何等待，失败**瞬时返回**（连超时都不等）。
    那样打 55 次和打 1 次没有区别，只是把日志刷爆 + 白烧 CPU。

    **为什么加抖动**：`2→4→8` 这种写死的节奏在多个客户端同时失败时会让它们
    **同步**重打（thundering herd）。业界量化结论是加抖动可减少 60~80% 的
    重试风暴。这里用 AWS 的 decorrelated jitter 形式：下界固定 `base`、
    上界随尝试次数指数放大到 `cap`，取值在区间内随机。

    ⚠️ 与 `app/retry.py` 的退避**分工不同，不要合并**：那个在**一次调用内部**
    重试时用（`retry_delay * 2**i`，进程内单次请求，没有同步问题，故不加抖动）；
    这个在**批与批之间**用。且 `news` 分组配了备用模型后 `llm_retry_times()`
    降为 1，内部退避**根本不会执行** —— 那条路指望不上（B1 的根因 2）。
    """
    return random.uniform(base, min(cap, base * (3 ** attempt)))
DUP_THRESHOLD = 0.85  # 相似度 ≥ 此值判为重复（**直接合并，不过 LLM**）
# 「近阈值」带的宽度（L1 标定观测，2026-09-24，应复核意见 §7.3 而加）。
#
# **为什么要观测它**：`DUP_THRESHOLD` 之上是**直接合并、不过 LLM** 的快路径，
# 阈值本身安不安全，只看「灰区花了多少钱」（§13.3 记的那两个数）是答不出来的。
# 复核方 2026-09-24 实测：本项目最容易误合并的模板化快讯（不同期货品种 / 不同 ETF /
# 不同公司）相似度最高 **0.8450** —— 距阈值只有 **0.005**，而且它们的比对发生在
# **本轮内聚簇**，不走「库内已有」那条路（详见复核意见 §7.3）。
#
# 宽度取 0.03：够窄，只捞得到真正贴脸的对；够宽，不至于常年为 0 而没有基线。
# **跟着阈值走**（`NEAR_MISS_LOW = DUP_THRESHOLD - 宽度`），阈值若将来抬高，观测带自动跟随。
NEAR_MISS_MARGIN = 0.03
NEAR_MISS_LOW = DUP_THRESHOLD - NEAR_MISS_MARGIN
# 语义聚簇的**规模上限**（L1，2026-09-24）。
#
# union-find 的传递闭包有个已知的**渗流相变**：只要 A–B 与 B–C 都超阈值，
# A、C 就被并进同一簇 —— 哪怕它俩的相似度远低于阈值。平时无害（簇小、成员确实相邻），
# 但同一模板的快讯一旦连发（「XX 股涨停」几十条），**阈值不变**也可能突然冒出一个
# 巨型连通分量，把大量真实信息并成一条 —— 而合并是**不可逆**的。
#
# 取 20 的依据：实测 source_count 分布 1源 607 / 2源 178 / 3源 140 ——
# 库里最多 3 个源，单事件的簇大小在个位数，20 已是两个数量级的余量。
# 撞上限时**拒绝自动合并**（降级为逐条），代价只是多花分类的钱，
# 而误合并永久丢信息 —— 代价不对称，所以宁可不合。
MAX_CLUSTER_SIZE = 20
GRAY_LOW = 0.70       # 相似度在此区间则交 LLM 复核（保持原行为）
# 语义去重的时间窗。**没有它就会过度合并** —— 详见 `_nearest_existing`。
DEDUP_WINDOW_HOURS = 24
FINGERPRINT_CONTENT_CHARS = 200  # 确定性指纹取正文前多少字
EMBED_CHUNK = 256     # numpy 相似度矩阵的分块行数（控内存峰值）
QUERY_CHUNK = 256     # Chroma 批查的分块数
SEMANTIC_NEIGHBORS = 5  # 每个候选取几个向量近邻，再在时间窗内挑最相似的

# 分类标签归一化（LLM 输出可能漂移到预设列表之外）
# 8 类：宏观/政策/行业/公司/国际/资金/市场/综合
ALLOWED_CATEGORIES = {"宏观", "政策", "行业", "公司", "国际", "资金", "市场", "综合"}
CATEGORY_ALIAS = {
    "策略": "综合", "大宗商品": "宏观", "商品": "宏观",
    "债券": "宏观", "汇率": "国际", "科技": "行业", "消费": "行业",
    "金融": "宏观", "房地产": "行业", "能源": "行业", "医药": "行业",
    "其他": "综合", "快讯": "综合", "财经": "综合",
}
ALLOWED_MARKETS = {"A股", "美股", "港股", "全球", "无"}
MARKET_ALIAS = {
    "中国": "A股", "国内": "A股", "大陆": "A股", "美国": "美股",
    "香港": "港股", "国际": "全球",
}


def _normalize_category(cat) -> str:
    """归一化类别。**兜底为「综合」而非「行业」**——避免行业成为垃圾桶。"""
    cat = (cat or "").strip()
    if cat in ALLOWED_CATEGORIES:
        return cat
    return CATEGORY_ALIAS.get(cat, "综合")


def _normalize_market(mkt) -> str:
    mkt = (mkt or "").strip()
    if mkt in ALLOWED_MARKETS:
        return mkt
    return MARKET_ALIAS.get(mkt, "无")


def _extract_json(text: str):
    """从 LLM 输出中容错提取 JSON（优先整个解析，失败则抓 [..] 片段）。"""
    text = (text or "").strip()
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ================= 步骤①：LLM 相关性分类 =================
def classify_news(items: list[NewsItem]) -> tuple[list[dict], list[NewsItem]]:
    """LLM 批量分类。返回 `(成功分类的结果, 失败批次的原始条目)`。

    **第二个返回值是 2026-09-22 加的，它修的是一个会丢数据的洞**：
    在此之前，某批分类失败后那些条目**既不入库、也不计入 `dropped`** ——
    日志会显示「丢弃 0」，而实际上整批没了。2026-09-21 实机发生过一次
    （百炼额度耗尽，一轮 1820 条新闻全部未入库，日志却写「丢弃 0」）。

    交出失败条目之后，上游才能①如实计数、②**拒绝推进水位线**（下一轮重取同一窗口）。
    """
    if not items:
        return [], []
    llm = get_llm(temperature=0.0, part="news")
    # 逐批打印进度：分类整批可能跑十几分钟，不打日志的话外部完全看不到进展
    total_batches = (len(items) + BATCH_SIZE - 1) // BATCH_SIZE
    logger.info(f"[分类] 共 {len(items)} 条，分 {total_batches} 批")
    results: list[dict] = []
    failed: list[NewsItem] = []
    consecutive_fails = 0
    for batch_idx, i in enumerate(range(0, len(items), BATCH_SIZE), start=1):
        batch = items[i:i + BATCH_SIZE]
        news_list = "\n".join(
            f"{j + 1}. {it.title}｜{(it.content or '')[:100]}"
            for j, it in enumerate(batch)
        )
        prompt = CLASSIFY_PROMPT.format(news_list=news_list)

        def _classify(p: str) -> list:
            d = _extract_json(llm.invoke(p).content)
            if not isinstance(d, list):
                raise ValueError("LLM 分类输出非数组")
            return d

        try:
            data = call_with_retry(_classify, prompt, retry_label="新闻分类",
                                   retry_times=llm_retry_times(part="news"))
            # 先攒在 batch_ok 里，成功走完才并入 results ——
            # 否则「循环中途抛异常」时，已 append 进 results 的条目会在 except 里
            # 被整批再算一次 failed，同一条既算成功又算失败。
            batch_ok: list[dict] = []
            covered: set[int] = set()
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                idx = int(entry.get("id", -1)) - 1
                if 0 <= idx < len(batch):
                    covered.add(idx)
                    themes = entry.get("themes") or []
                    if isinstance(themes, str):
                        themes = [themes]
                    batch_ok.append({
                        "item": batch[idx],
                        "relevant": bool(entry.get("relevant", False)),
                        "category": _normalize_category(entry.get("category")),
                        "market": _normalize_market(entry.get("market")),
                        "themes": [str(t) for t in themes],
                    })
            results.extend(batch_ok)
            # ⚠️ **只堵「整批异常」是不够的**（2026-09-22 巡检发现）：
            # LLM 完全可能返回一个**合法但条目变少**的列表（漏项、id 越界、id 重复），
            # 那些没被覆盖到的条目若不在这里交出来，就既不进 results 也不进 failed
            # → `classify_failed` 仍是 0 → 水位线照常推进 → **永久丢失**。
            missed = [it for i, it in enumerate(batch) if i not in covered]
            if missed:
                failed.extend(missed)
                logger.warning(f"LLM 分类**漏项** {len(missed)}/{len(batch)} 条"
                               f"（输出条目不全，非异常）—— 已计入分类失败")
        except Exception as e:  # noqa: BLE001
            # 交出来而不是丢掉 —— 上层要据此拒绝推进水位线（详见函数 docstring）
            failed.extend(batch)
            consecutive_fails += 1
            # 日志合并成**一行**（B1 ③）：原先是「`[重试] …` + `LLM 分类失败…`」两行，
            # 55 批就是 110 行，把别的信息全淹了。退避信息也并进这一行 ——
            # 单独打会显得零碎，读者要拼三行才知道发生了什么。
            retryable = is_retryable(e)
            breaking = consecutive_fails >= MAX_CONSECUTIVE_BATCH_FAILS
            # 末批不退避：后面已经无批可打，等下来的时间纯属浪费（巡检 2026-09-24 指出）。
            last_batch = batch_idx >= total_batches
            will_sleep = retryable and not breaking and not last_batch
            delay = _backoff_delay(consecutive_fails) if will_sleep else 0.0
            if not retryable:
                # 到熔断阈值时要说清「下一步是中断」，否则读者看到「不退避」还得
                # 往下读一行才知道整轮要停了
                note = ("**不可重试**（鉴权/参数/配额类），不退避"
                        + ("，中断本轮" if breaking else ""))
            elif breaking:
                note = "**可重试**，但已连续失败到熔断阈值 —— 不再退避，直接中断"
            elif last_batch:
                note = "**可重试**，但已是最后一批 —— 不退避（后面无批可打）"
            else:
                note = f"**可重试**，退避 {delay:.1f}s 后打下一批"
            logger.warning(
                f"[分类] 第 {batch_idx}/{total_batches} 批失败"
                f"（{len(batch)} 条未分类）：{e}｜{note}（连续失败 {consecutive_fails} 次）"
            )
            if breaking:
                # **熔断**：LLM 连续失败通常意味着"不可用"（连接断了 / 额度耗尽 /
                # key 失效），而不是"这一批运气不好"。此时把剩下的批次全打完，
                # 只是白等 + 把日志刷爆（2026-09-22 实测：连接中断后 **0.43 秒内
                # 刷出 110 条报错**，55 批全废）。
                #
                # ⚠️ 中断时**必须把剩余条目也交出来** —— 否则它们既不在 results
                # 也不在 failed，`classify_failed` 会是 0，水位线照常推进，
                # 那批新闻**永久丢失**。这就又回到了 §17 要堵的那个洞。
                rest = items[i + BATCH_SIZE:]
                failed.extend(rest)
                logger.error(
                    f"[分类] 连续 {consecutive_fails} 批失败，判定 LLM 不可用 —— "
                    f"**中断本轮分类**，剩余 {len(rest)} 条一并计入分类失败"
                    f"（水位线不推进，下轮重取）"
                )
                break
            if will_sleep:
                # 只对 transient 退避：对 401/403/400 等待是纯白等
                # （判据复用 `app.retry.is_retryable`，不在这里重写一份）。
                time.sleep(delay)
        else:
            consecutive_fails = 0
            # 日志 4/5：进度行补上「成功 / 漏项 / 模型」。
            # 模型名是判断「这批是否悄悄走了备用」的唯一线索 ——
            # `with_fallbacks` 默认静默切换，只在切走时打一条 WARNING，
            # 而成功的批次原先完全看不出用的是谁。
            # ⚠️ 「成功 / 漏项」必须按 `covered` 计数，**不能用 `len(batch_ok)`** ——
            # LLM 返回重复 id 时 `batch_ok` 会照收多条（同一 id 反复追加），
            # 于是 `len(batch) - len(batch_ok)` 会少报甚至**为负**，还会与同一批
            # 那条 `len(missed)/len(batch)` 的 warning 自相矛盾。
            # 实测（巡检复现）：批 2 条、LLM 返回 3 条 `id=1` → 日志「成功 3 / 漏项 -1」。
            logger.info(
                f"[分类] 第 {batch_idx}/{total_batches} 批："
                f"成功 {len(covered)} / 漏项 {len(missed)}"
                f" / 模型 {get_llm_model_name(part='news')}"
            )
    return results, failed


# ================= 步骤①：确定性去重（免费） =================
@dataclass
class Candidate:
    """本轮的一个**去重单元**：正文完全相同的若干条目合成一个。

    为什么需要它：`_dedup_by_url` 只认 url，**url 为空的条目完全没有去重保护**
    （实测库里有 158 条 url 为空），而同轮内它们是**互相看不见**的 ——
    语义比对依赖 Chroma，而本批新增要等索引之后才可见。
    实测最典型的一组：同一标题 5 行、`collected_at` 精确到同一微秒、url 全为 None。
    同指纹先并成一个候选，这一类就消失了。
    """
    item: NewsItem                                  # 代表条目（正文最长的那个）
    sources: list[tuple[str, str | None]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _doc_text(self.item.title, self.item.content)


def _norm(s: str) -> str:
    """归一化：去首尾空白 + 把连续空白（含换行）折叠成一个空格。"""
    return _WS.sub(" ", (s or "").strip())


def _fingerprint(title: str, content: str) -> str:
    """确定性去重键：归一化标题 + **正文前 200 字** 的 sha1。

    ⚠️ **为什么必须带正文，而不是「标题相同就算重复」** —— 实测反例：
    《轻工纺织产业发展“十五五”规划》解读 在库里 6 行，正文分别是「总体考虑」
    「提升创新能力」「激发融合化发展动力」「加快智能化绿色化转型」「增强供需适配性」
    等**不同问答**，只有其中 2 行正文真的一样。只按标题去重会**误杀 5 条真实信息**。

    所以这里**不加时间窗**：① 只在**本轮之内**分组（一轮跨度约 75 分钟），天然同窗；
    而「南向资金净买入额达30亿港元」那种跨天重发、正文一字不差的快照，
    改由 ② 的 `DEDUP_WINDOW_HOURS` 挡住。
    """
    head = _norm(content)[:FINGERPRINT_CONTENT_CHARS]
    raw = f"{_norm(title)}\x00{head}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _group_by_fingerprint(items: list[NewsItem]) -> list[Candidate]:
    """把正文完全相同的条目并成一个候选，保留全部 (source, url)。"""
    groups: dict[str, Candidate] = {}
    for it in items:
        fp = _fingerprint(it.title, it.content or "")
        c = groups.get(fp)
        if c is None:
            groups[fp] = Candidate(item=it, sources=[(it.source, it.url)])
            continue
        c.sources.append((it.source, it.url))
        # 代表条目取**正文最长**的 —— 信息更全，分类与嵌入都用它
        if len(it.content or "") > len(c.item.content or ""):
            c.item = it
    return list(groups.values())


def _distinct_sources(pairs) -> int:
    """`source_count` = **去重后的源数**（不是「(源, url) 对数」）。

    2026-09-22 修正（用户拍板）：原先数的是「来源对」，于是一篇稿子在同一家
    挂了两个 url 就变成「2源」。**实测库里有 332 行标着「2源」其实只有 1 个源** ——
    而 `source_count` 会喂给 `assess_info_level` 的信号②（门槛 ≥2），
    并在专家 prompt 里显示成「（N源）」，虚高会同时误导这两处。
    字段名与 `AGENTS_DESIGN.md` §2.6（「几个源报道」）本来指的就是源数，
    这里让实现向文档靠拢。
    """
    return len({s for s, _ in pairs if s}) or 1


def _dedup_sources(sources: list[tuple[str, str | None]]
                   ) -> list[tuple[str, str | None]]:
    """同一候选内按 (source, url) 去重，保持出现顺序。

    无 url 的重复条目会折叠成一条 —— 这与库内既有的 `source_count` 口径一致
    （`_merge_into` 用的也是 (source, url) 集合），不会凭空抬高源数。
    """
    seen: set[tuple[str, str | None]] = set()
    out: list[tuple[str, str | None]] = []
    for pair in sources:
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


# ================= 步骤②：批量嵌入 + 语义近邻 =================
def _embed_candidates(cands: list[Candidate]) -> np.ndarray:
    """批量嵌入候选代表条目，返回**已 L2 归一化**的 (n, d) 矩阵。

    归一化是为了后面用内积直接当余弦相似度，省掉重复的模长计算。
    """
    if not cands:
        return np.zeros((0, 1024), dtype=np.float32)
    vecs = np.asarray(embed_documents([c.text for c in cands]), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0          # 全零向量不该出现，但别让它把结果变成 nan
    return vecs / norms


def _peek(text: str, n: int = 20) -> str:
    """日志里引用一段文本时的**唯一**写法：压掉换行、截断。"""
    return _WS.sub(" ", (text or "")).strip()[:n]


def _cluster_by_similarity(vecs: np.ndarray,
                           threshold: float = DUP_THRESHOLD,
                           chunk: int = EMBED_CHUNK,
                           texts: list[str] | None = None) -> list[list[int]]:
    """按余弦相似度 ≥ threshold 把候选聚成簇（union-find），返回下标分组。

    为什么要自己做这一步：本轮新增的条目要等入库后才进 Chroma，所以**同轮内
    跨源的同一事件互相看不见**。实测反例：「海光信息将发布新品类芯片」三个源
    各一行、url 齐全、`collected_at` 同一秒，**却没合并** —— 因为它们彼此不可见。

    分块计算：n 条候选的相似度矩阵是 n²，2200 条就是 480 万个数；回补场景可能
    上万条（10⁸），一次性算完内存吃不消。按 `EMBED_CHUNK` 行一块块来，
    且只与 `i` 之后的比（对称矩阵只用一半）。

    返回前做两件事（L1，2026-09-24）：**记录簇大小分布**（观测）与
    **拆掉超过 `MAX_CLUSTER_SIZE` 的簇**（保护）。后者会让返回值比「纯 union-find
    的结果」更碎 —— 这是刻意的：漏合并只多花分类的钱，误合并永久丢信息。

    `texts`：与 `vecs` 同序的原文，**只用于日志示例**（`None` 则示例退化为下标）。
    **签名保持向后兼容** —— 既有调用方与契约脚本只传 `vecs`。

    另外记录**阈值两侧的相似度分布**（2026-09-24，复核意见 §7.3）：
    「近阈值对」（**两两直接**相似度落在 `[NEAR_MISS_LOW, threshold)`）与
    「簇内最不像的一对」（真重复里离阈值最近的那对）。**只有一侧选不了阈值**：
    只知道「最像的假对是 0.845」而不知道「最不像的真对是多少」，就无从判断
    0.005 的余量到底是紧还是宽。

    ⚠️ 「近阈值对」**不等于**「差点被误合并」：传递闭包并进同一簇的两条，
    彼此直接相似度可能落在带内（A–B、B–C ≥0.85 而 A–C 只有 0.83）。
    日志措辞已写明是「**直接**相似度」，别把它读成「这一对被单独差点合并」。
    """
    n = len(vecs)
    if n <= 1:
        return [[i] for i in range(n)]
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]      # 路径压缩
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    nm_count = 0                               # 近阈值对的总数
    nm_top: list[tuple[float, int, int]] = []  # 每块只留 top-3，合并后仍是全局 top-3
    for i in range(0, n, chunk):
        sims = vecs[i:i + chunk] @ vecs[i:].T   # (c, n-i)
        rows, cols = np.where(sims >= threshold)
        for r, k in zip(rows.tolist(), cols.tolist()):
            if k > r:                          # 跳过对角线与重复的那一半
                union(i + r, i + k)
        # 近阈值对：与上面**同一遍枚举**（都要求 `k > r`），所以每对只数一次。
        # 刻意用 numpy 取值、每块只留 top-3 —— 近阈值对可能有几千对，
        # 在 Python 层逐对展开会同时吃掉 CPU 和日志（第 5 批刚做完日志降噪）。
        nr, nc = np.where((sims >= NEAR_MISS_LOW) & (sims < threshold))
        keep = nc > nr
        nr, nc = nr[keep], nc[keep]
        if nr.size:
            nm_count += int(nr.size)
            vals = sims[nr, nc]
            take = (np.argpartition(vals, -3)[-3:] if vals.size > 3
                    else np.arange(vals.size))
            for t in take:
                nm_top.append((float(vals[t]), i + int(nr[t]), i + int(nc[t])))
    buckets: dict[int, list[int]] = {}
    for idx in range(n):
        buckets.setdefault(find(idx), []).append(idx)
    clusters = list(buckets.values())

    # ③ 分布观测：**一个数字就能报警**。刻意统计**保护前**的原始分布 ——
    # 拆过之后再统计就永远看不到那个异常的大簇了，而「最大簇从 14 跳到 9000」
    # 正是渗流相变唯一的早期信号。
    sizes = sorted(len(c) for c in clusters)
    if sizes:
        p95 = sizes[min(len(sizes) - 1, int((len(sizes) - 1) * 0.95))]
        logger.info(
            f"[去重] 簇大小分布：组数 {len(sizes)}｜最大 {sizes[-1]}"
            f"｜p95 {p95}｜中位 {sizes[len(sizes) // 2]}"
        )

    # ① 规模保护：超限的簇**拒绝自动合并**，降级为逐条处理。
    # 降级的代价只是「本可以省下的几次分类」，而放它合并的代价是**永久丢信息**
    # —— 不对称，所以宁可不合（详见 `MAX_CLUSTER_SIZE` 的注释）。
    out: list[list[int]] = []
    for c in clusters:
        if len(c) > MAX_CLUSTER_SIZE:
            logger.warning(
                f"[去重] 候选簇规模 {len(c)} 超过上限 {MAX_CLUSTER_SIZE} —— "
                f"**拒绝自动合并**，降级为逐条（多花分类的钱，不丢信息）。"
                f"这通常是模板化快讯连发的征兆（传递闭包渗流），"
                f"请对照本行的簇大小分布确认最大簇是否异常"
            )
            out.extend([i] for i in c)
        else:
            out.append(c)

    # 重复侧：每个多元素簇**内部最不像的一对**（簇内最小两两相似度）。
    # 在**保护后**的簇上算 —— 每个簇 ≤ MAX_CLUSTER_SIZE，故 Σk² ≤ 20n，代价可忽略；
    # 若在保护前算，一个 2000 条的巨型簇就是 4M 个浮点。
    dup_min: float | None = None
    dup_clusters = 0
    chain_suspects = 0                                # 簇内最小两两 < GRAY_LOW 的簇数
    worst: tuple[float, list[int], int, int] | None = None   # 最差那簇：(值, 成员, 局部 i, j)
    for c in out:
        if len(c) < 2:
            continue
        dup_clusters += 1
        sub = vecs[c] @ vecs[c].T
        np.fill_diagonal(sub, 1.0)     # 对角线上是同一条自己，不能算「最不像的一对」
        # **找出是哪一对**最不像 —— 只报一个数值证明不了合得对不对：
        # 同一事件的「长文 + 短快讯」也可能只有 0.70。要有标题才能人工判。
        li, lj = np.unravel_index(int(np.argmin(sub)), sub.shape)
        m = float(sub[li, lj])
        if m < GRAY_LOW:
            # 直接比对会判「不重复」（< GRAY_LOW），却因传递闭包被并进同一簇 ——
            # 这是链式漂移最硬的判据，**不需要引入任何新的魔法数字**。
            chain_suspects += 1
        if dup_min is None or m < dup_min:
            dup_min = m
            worst = (m, c, int(li), int(lj))

    nm_max = max((t[0] for t in nm_top), default=None)
    logger.info(
        # ⚠️ 措辞里必须有「**直接**相似度」：`nm_count` 数的是**两两直接**落在
        # [NEAR_MISS_LOW, threshold) 的对，**不等于**「差点被误合并」——
        # 经传递闭包已并进同一簇、但彼此直接相似度偏低的那些对也会被计入
        # （A–B、B–C 都 ≥0.85 时，A–C 可能只有 0.83）。巡检 2026-09-24 指出。
        f"[去重] 阈值余量：近阈值对 {nm_count} 对"
        f"（**直接**相似度 ≥{NEAR_MISS_LOW:.2f} 且 <{threshold:.2f}）"
        + (f"，最高的那对 {nm_max:.4f}（距阈值还差 {threshold - nm_max:.4f}）"
           if nm_max is not None else "")
        + f"｜合并簇 {dup_clusters} 个"
        + (f"，簇内最不像的一对 {dup_min:.4f}（高出阈值 {dup_min - threshold:+.4f}"
           + (f"；**{chain_suspects} 个簇里存在低于灰区下限 {GRAY_LOW} 的一对**"
              if chain_suspects else "")
           + "）"
           if dup_min is not None else "")
    )
    # ⚠️ **链式合并取证**（2026-09-24，实测数据触发）：只在真出现可疑簇时打，
    # 且**带标题**。生产日志已出现「簇内最不像的一对 0.6921」—— 合并只发生在
    # 直接相似度 ≥0.85 的对上，所以那是 A–B–C 链条而 A–C 只有 0.69。
    # 但**那不等于合错了**：同一事件的「长文 + 短快讯」也可能只有 0.70。
    # 所以这一行的用途是**交出证据让人判**，不是自动改判定。
    if chain_suspects and worst is not None:
        _m, _members, _li, _lj = worst
        _a, _b = _members[_li], _members[_lj]
        _peek_a = _peek(texts[_a]) if texts and _a < len(texts) else f"#{_a}"
        _peek_b = _peek(texts[_b]) if texts and _b < len(texts) else f"#{_b}"
        logger.warning(
            f"[去重] ⚠️ 链式合并可疑：{chain_suspects} 个合并簇内存在相似度低于 "
            f"{GRAY_LOW} 的一对（**直接比对本会判「不重复」**）｜最差那簇 {len(_members)} 条，"
            f"{_m:.4f}「{_peek_a}」≈「{_peek_b}」—— 请人工确认这两条是否同一事件"
        )
    # 示例单独一行、**只在真有近阈值对时打** —— 安静轮次不该多出一行。
    if nm_top and texts:
        examples = [
            f"{s:.4f}「{_peek(texts[a])}」≈「{_peek(texts[b])}」"
            for s, a, b in sorted(nm_top, reverse=True)[:3]
            if a < len(texts) and b < len(texts)   # texts 比 vecs 短时不许越界
        ]
        if examples:
            logger.info("[去重] 近阈值对示例：" + "；".join(examples))
    return out


def _collapse_clusters(cands: list[Candidate],
                       clusters: list[list[int]]) -> list[int]:
    """把同簇候选并成一个；**返回每个新候选的代表在旧列表里的下标**。

    返回**下标**而不是新建对象，是为了让调用方**复用首轮已经算好的向量** ——
    合并后候选的 `item` 就是簇代表那一条，文本一字未变，`vecs[rep]` 直接可用。

    ⚠️ 改动前这里返回新建的 `Candidate`，调用方拿不到对应向量，只能对全量重算一次
    （`新闻采集入库索引已知问题.md` §8.8 的 M3）。2026-09-23 实机：该步 22s，
    其中那次全量重算是**纯冗余** —— 文档 §10.2 的定性是对的，这里把量也补上了。

    **副作用**：会就地把 `cands[rep]` 换成合并后的对象（代表下标不变）。
    """
    reps: list[int] = []
    for idxs in clusters:
        if len(idxs) == 1:
            reps.append(idxs[0])
            continue
        rep = max(idxs, key=lambda i: len(cands[i].item.content or ""))
        merged = Candidate(item=cands[rep].item, sources=[])
        for i in idxs:
            merged.sources.extend(cands[i].sources)
        cands[rep] = merged
        reps.append(rep)
    return reps


def _nearest_existing(db: Session, cands: list[Candidate],
                      vecs: np.ndarray) -> list[tuple[News, float] | None]:
    """一次 Chroma 批查，返回每个候选在**时间窗内**最相似的库内新闻（或 None）。

    **时间窗（`DEDUP_WINDOW_HOURS`）是 2026-09-22 加的，修的是「过度合并」**：
    本系统只有 3 个源，所以 `source_count > 3` 结构上不可能是单一事件 ——
    但实测库里有 **416 行（6%）`source_count ≥ 4`，最高 33**。样本：

        南向资金净买入额达30亿港元   33 个 url，横跨 09-06 ~ 09-22
        现货黄金失守4300美元/盎司    30 个，同标题不同天反复出现

    这些是**反复出现的行情快照**，标题正文都一字不差，但**是不同时刻的不同事实**。
    没有时间窗时它们被合并成一行，后果是 `source_count` 虚高 ——
    而它会喂给 `assess_info_level` 的信号②，并在专家 prompt 里显示成
    「（33源）」这种**失真输入**。

    取 `SEMANTIC_NEIGHBORS` 个近邻再在窗口内挑最相似的，而不是只取 top-1：
    最近邻可能恰好落在窗口外，多取几个才有机会找到真正该合并的那条。
    """
    n = len(cands)
    if n == 0:
        return []
    coll = vector_store.get_collection()
    if coll.count() == 0:
        return [None] * n

    window = timedelta(hours=DEDUP_WINDOW_HOURS)
    # 日志 3：这段改动前是一个 86 秒的黑盒（实测 18:00:43 → 18:02:09），
    # 里面混着 Chroma 批查 / DB 取行 / 近邻筛选三件事，日志分不出占比。
    t_chroma_start = time.time()
    id_lists: list[list[str]] = []
    dist_lists: list[list[float]] = []
    for st in range(0, n, QUERY_CHUNK):         # 分块查，控内存峰值
        res = coll.query(
            query_embeddings=[v.tolist() for v in vecs[st:st + QUERY_CHUNK]],
            n_results=SEMANTIC_NEIGHBORS,
        )
        id_lists.extend(res.get("ids") or [])
        dist_lists.extend(res.get("distances") or [])

    t_chroma = time.time() - t_chroma_start
    all_ids = {i for lst in id_lists for i in lst}
    rows: dict[str, News] = {}
    t_db_start = time.time()
    if all_ids:
        rows = {r.id: r for r in db.query(News).filter(News.id.in_(all_ids)).all()}
    t_db = time.time() - t_db_start

    out: list[tuple[News, float] | None] = []
    for idx, c in enumerate(cands):
        ref = c.item.publish_time or datetime.now()
        ids = id_lists[idx] if idx < len(id_lists) else []
        dists = dist_lists[idx] if idx < len(dist_lists) else []
        best: tuple[News, float] | None = None
        for k, nid in enumerate(ids):
            news = rows.get(nid)
            if news is None:
                continue
            if abs((news.publish_time or ref) - ref) > window:
                continue                    # 窗口外 → 不是同一事件，继续看下一个近邻
            sim = 1.0 - float(dists[k]) if k < len(dists) else 0.0
            if best is None or sim > best[1]:
                best = (news, sim)
        out.append(best)
    logger.info(f"[去重] 语义比对耗时：Chroma 批查 {t_chroma:.1f}s"
                f" / DB 取行 {t_db:.1f}s"
                f" / 近邻筛选 {time.time() - t_chroma_start - t_chroma - t_db:.1f}s"
                f"（{n} 个候选，{len(all_ids)} 个邻居 id）")
    return out


def _fmt_for_dedup(title: str, content: str | None) -> str:
    """判重时喂给 LLM 的单条文本 —— **两侧必须同形态**。

    改动前是 A 只给 `title`、B 给 `_doc_text(...)[:150]`（即 `标题\n正文`），两个毛病：

    ① **不对等**：一侧有正文、一侧没有，LLM 拿到的信息量差一个量级；
    ② **换行撑破 prompt 结构**：`DEDUP_PROMPT` 是
       `新闻A：[源] {title_a}` / `新闻B：[源] {title_b}` 两行，
       把带 `\n` 的文本塞进 `{title_b}` 后，「新闻B：」那一行之后会多出
       一整段**游离文本** —— 不在任何标签之下，LLM 很可能把它当成 prompt 的
       额外指令或上下文，而不是新闻 B 的内容。

    现在两侧都走这个函数：`标题｜正文前 120 字`（空白已折叠成单空格，不含换行）。
    """
    body = _norm(content or "")[:120]
    return f"{title}｜{body}" if body else title


def _normalize_verdict(text: str | None) -> bool | None:
    """把 LLM 的 true/false 响应规范化成三态：`True` / `False` / **`None`（拿不准）**。

    **容错的只有「格式」**（这些都是同一个语义，只是包裹不同）：
    首尾空白、` ``` ` 代码围栏、尾部句号/冒号、大小写、外层引号。

    **绝不做子串匹配** —— 那是 2026-09-25 修掉的毛病：
    `not true`（语义相反）、`untrue`（子串命中）、`true. 因为…`（带解释）
    在原实现下**全部会被判成「重复」**，而误判的代价是**真实新闻被永久合并**，
    与 fail-open 的方向正好相反。拿不准就返回 `None`，交上层当新条目处理。
    """
    s = (text or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()   # 去代码围栏
    s = s.strip("。.：:；; \t\"'“”").lower()
    if s == "true":
        return True
    if s == "false":
        return False
    return None


def _llm_is_duplicate(a: News, cand: Candidate) -> bool | None:
    """灰色区间复核：LLM 判断**库内已有那条**与**本轮候选**是否同一事件。

    返回三态：`True` 判为重复 / `False` 判为新 / **`None` 表示调用失败**。

    ⚠️ 失败时上层按「不是重复」处理（= 当新条目入库）。这是**刻意的 fail-open**：
    与采集层「水位线不推进」同向 —— 宁可多一条重复，也不要把真新闻当重复吞掉，
    因为后者不可逆。但**不再静默**：改动前这里是裸的 `except: return False`，
    连一行日志都没有，LLM 一挂灰区条目就悄悄全变成新行、无人知晓。
    """
    llm = get_llm(temperature=0.0, part="news")
    # ⚠️ 两侧同形态（见 `_fmt_for_dedup`）—— 改动前 A 只给标题、B 给「标题+正文」，
    # 且 B 里的换行会把 prompt 结构撑开。那等于**拿不对等的输入让 LLM 做判断**。
    prompt = DEDUP_PROMPT.format(
        source_a=a.source,
        title_a=_fmt_for_dedup(a.title, a.content),
        source_b=cand.item.source,
        title_b=_fmt_for_dedup(cand.item.title, cand.item.content),
    )
    try:
        resp = llm.invoke(prompt)
        # ⚠️ **精确匹配，不用子串**（2026-09-25 修，第三方巡检 L1）。
        # 原写法 `"true" in resp.lower()` 会把这些**全部**误判成「重复」：
        #   · `not true`        —— 语义恰好相反
        #   · `true. 因为…`     —— 带解释（模型没严格守「只输出 true/false」）
        #   · `untrue`          —— 子串命中
        # 误判的代价是**把真实新闻永久合并掉**（不可逆），方向与 fail-open 相反。
        # 现在：只接受规范化后**恰好**是 true / false；其余一律返回 None
        # （= 调用失败），沿用上层的 fail-open「当新条目入库」，并留下日志。
        verdict = _normalize_verdict(resp.content)
        if verdict is None:
            logger.warning(
                f"灰区判重返回了非 true/false 的内容，按「不是重复」处理"
                f"（宁可多存一条）：{(resp.content or '')[:80]!r}"
            )
        return verdict
    except Exception as e:  # noqa: BLE001
        logger.warning(f"灰区判重调用失败，按「不是重复」处理（宁可多存一条）: {e}")
        return None


# ================= 步骤③：多源合并 =================
def _merge_into(db: Session, existing: News, cand: Candidate) -> bool:
    """把候选并入已有记录：累加 source_count / source_urls。

    返回**正文是否被更新**（P2 修 split-brain 用）。改动前 `_upsert_index` 只对
    **新增**条目调用，而这里更新了 `title`/`content` 之后 **Chroma 里仍是旧文本**；
    BM25 却每轮从 DB 全量重建 —— 于是**两套索引对同一条文档版本不一致**：
    向量召回用旧文、关键词召回用新文，RRF 融合的排名因此错乱。
    """
    urls: list[dict] = []
    if existing.source_urls:
        try:
            urls = json.loads(existing.source_urls)
        except json.JSONDecodeError:
            urls = []
    all_sources = {(existing.source, existing.url)} | {
        (u.get("source"), u.get("url")) for u in urls if isinstance(u, dict)
    }
    # 只收**带 url** 的来源对 —— 与改动前 `if item.url` 的口径一致。
    # 否则同源的无 url 重复会以 (source, None) 混进来，凭空抬高 source_count。
    all_sources |= {(src, u) for src, u in _dedup_sources(cand.sources) if u}
    existing.source_urls = json.dumps(
        [{"source": s, "url": u} for s, u in all_sources if s],
        ensure_ascii=False,
    )
    # ⚠️ 数**去重源数**，不是 len(all_sources)（那是「来源对」数，会虚高）
    existing.source_count = _distinct_sources(all_sources)
    # 信息更全（正文更长）则更新主内容
    if len(cand.item.content or "") > len(existing.content or ""):
        existing.title = cand.item.title
        existing.content = cand.item.content
        return True
    return False


def _insert_new(db: Session, cand: Candidate, cls: dict) -> News:
    """插入新新闻（带分类标签 + 候选携带的全部来源）。"""
    pairs = [(src, u) for src, u in _dedup_sources(cand.sources) if u]
    item = cand.item
    n = News(
        title=item.title,
        content=item.content or "",
        source=item.source,
        # 代表条目未必带 url（同指纹里正文最长的那个可能是无 url 的），
        # 从候选的来源对里取第一个有 url 的，别让前端的「原文」链接凭空消失
        url=next((u for _s, u in pairs if u), None),
        publish_time=item.publish_time or datetime.now(),
        category=cls["category"],
        market=cls["market"],
        themes=json.dumps(cls["themes"], ensure_ascii=False),
        # 口径与 _merge_into 一致：**去重后的源数**。代表条目自己的来源也算，
        # 这样无 url 的单源条目仍然是 1（不凭空抬高）。
        source_count=_distinct_sources([(item.source, item.url), *pairs]),
        source_urls=json.dumps(
            [{"source": src, "url": u} for src, u in pairs], ensure_ascii=False
        ),
    )
    db.add(n)
    return n


# ================= 步骤⑤：索引 =================
def _upsert_index(news_list: list[News]) -> None:
    """把新闻写进 Chroma（新增与被改写的一起）。

    改名自 `_index_new` —— 旧名字「只索引新增」正是 split-brain 的成因，
    留着这个名字下一个读代码的人还会踩同一个坑。
    """
    if not news_list:
        return
    # 再兜一层去重：调用方已按 id 去重过 `changed`，但 `new_news` 内部理论上
    # 仍可能重复（`classify_news` 不去重 LLM 返回的重复 id，是历史遗留路径）。
    # Chroma 对重复 id 抛 DuplicateIDError，而这个异常在 db.commit() 之后 ——
    # 与其让整轮崩在半完成状态，不如在这里无声收敛。
    uniq: list[News] = []
    _seen: set[str] = set()
    for n in news_list:
        if n.id in _seen:
            continue
        _seen.add(n.id)
        uniq.append(n)
    news_list = uniq
    ids = [n.id for n in news_list]
    texts = [_doc_text(n.title, n.content) for n in news_list]
    t_embed = time.time()
    embeddings = embed_documents(texts)
    t_embed = time.time() - t_embed
    metadatas = [
        {
            "news_id": n.id,
            "title": n.title,
            "source": n.source,
            "publish_time": n.publish_time.isoformat() if n.publish_time else "",
            "category": n.category or "",
        }
        for n in news_list
    ]
    t_write = time.time()
    vector_store.upsert_documents(ids, texts, embeddings, metadatas)
    # 日志 1：改动前这里**一行日志都没有** —— Chroma 实际写了多少条、耗时多少、
    # 有没有异常，全看不见。P2（合并后重索引）的修复效果只能靠 DB 反推。
    logger.info(f"[索引] Chroma upsert {len(ids)} 条｜向量化 {t_embed:.1f}s"
                f" + 写入 {time.time() - t_write:.1f}s")


def _rebuild_bm25(db: Session, days: int = 7) -> None:
    since = datetime.now() - timedelta(days=days)
    news_list = db.query(News).filter(News.publish_time >= since).all()
    chunks = [
        {"id": n.id, "text": _doc_text(n.title, n.content),
         "metadata": {"news_id": n.id}}
        for n in news_list
    ]
    get_retriever().build_bm25_index(chunks)


# ================= 主流程 =================
def _dedup_by_url(items: list[NewsItem]) -> list[NewsItem]:
    """按 url 去重（翻页边界会产生重复 url），url 为空则不去重。"""
    seen: set[str] = set()
    result: list[NewsItem] = []
    for it in items:
        if it.url:
            if it.url in seen:
                continue
            seen.add(it.url)
        result.append(it)
    return result
def run_data_agent(db: Session, raw: list[NewsItem] | None = None) -> dict:
    """数据采集 Agent 主流程，返回统计 dict。

    `raw` 不为 None 时**不再自行采集**，直接用调用方给的条目 ——
    2026-09-22 起采集由 `news_service.run_collection` 负责，这样水位线才能
    「采集 → 分类 → 入库全部成功后才推进」。传 None 时保持旧行为（自采），
    供 `scripts/` 里的独立脚本使用。

    顺序见模块 docstring。返回值**保持既有键**（`collected` / `classified` /
    `relevant` / `dropped` / `classify_failed` / `new` / `merged`），
    另加几个观测字段。

    ⚠️ **口径变化**：`dropped` 与 `merged` 的含义随顺序调整而变 ——
    重复条目**不再进入分类**，所以它们既不会计入 `dropped`（判为不相关），
    也不会像以前那样"先分类成功再合并"。跨本次提交的历史统计**不可比**。
    """
    # 1. 采集（或接收调用方已采的数据）+ 按 url 去重（翻页会产生重复 url）
    if raw is None:
        # 顺序与改动前一致：仍是 新浪 → 东财 → 财联社 依次拼接
        raw = [it for r in collect_all_news() for it in r.items]
    raw = _dedup_by_url(raw)

    # 2. ①【几乎免费】确定性去重：正文相同的合成一个候选
    cands = _group_by_fingerprint(raw)
    exact_deduped = len(raw) - len(cands)

    # 3. ②【便宜】批量嵌入 + 语义近邻
    #
    # 这一段的耗时主要在 bge-m3 向量化（CPU）。改动前是**逐条** `embed_query`
    # （实测首轮 1853 次），现在整批一次编码，快得多 —— 所以"把去重提到前面"
    # 不会因为多编码一次而把省下的 LLM 时间吃掉。
    logger.info(f"[去重] 确定性去重：{len(raw)} 条 → {len(cands)} 个候选")
    _t_embed = time.time()
    vecs = _embed_candidates(cands)
    if len(vecs) != len(cands):
        logger.warning(
            f"[去重] 嵌入返回 {len(vecs)} 条，与候选数 {len(cands)} 不一致 —— "
            f"多出的候选拿不到近邻，会**一律按新条目处理**（宁可多存，不误合）"
        )
    # 传 texts 只为让「近阈值对示例」能打出标题 —— 那正是标定阈值时最该被看见的东西
    clusters = _cluster_by_similarity(vecs, texts=[c.text for c in cands])
    within_round = 0
    if len(clusters) != len(cands):
        before = len(cands)
        reps = _collapse_clusters(cands, clusters)
        # **复用首轮向量**（M3）：合并后候选的 item 就是簇代表，文本未变，
        # `vecs[rep]` 直接可用 —— 重算一次是纯冗余（2026-09-23 实测该步 22s）。
        vecs = vecs[reps]
        cands = [cands[i] for i in reps]
        within_round = before - len(cands)
    logger.info(f"[去重] 批量嵌入完成：{len(cands)} 个候选，"
                f"本轮内语义合并 {within_round} 个，耗时 {time.time() - _t_embed:.0f}s")

    matches = _nearest_existing(db, cands, vecs)

    to_merge: list[tuple[News, Candidate]] = []
    to_classify: list[Candidate] = []
    gray_failed = 0
    gray_calls = 0
    gray_secs = 0.0
    gray_merged = 0
    for cand, m in zip(cands, matches):
        if m is None or m[1] < GRAY_LOW:
            to_classify.append(cand)
        elif m[1] >= DUP_THRESHOLD:
            to_merge.append((m[0], cand))      # 高置信重复：**不过 LLM**
        else:
            gray_calls += 1
            _t_gray = time.time()
            verdict = _llm_is_duplicate(m[0], cand)  # 灰区
            gray_secs += time.time() - _t_gray
            if verdict is None:
                gray_failed += 1
            if verdict:
                gray_merged += 1
                to_merge.append((m[0], cand))
            else:
                to_classify.append(cand)

    # L3：真实省下的分类量 = 「不去重会分类的量」−「实际分类的量」。
    # 三段贡献相加恰好等于它（2026-09-23 那轮实测：52 + 727 + 106 = 885）。
    # ⚠️ 改动前这里只印 `len(to_merge)`（106），**低估约 8 倍** ——
    # 读日志的人得自己把三行进度日志相加才知道真实收益。
    saved = len(raw) - len(to_classify)
    logger.info(
        f"[去重] 语义比对后：{len(to_merge)} 个候选并入库内已有新闻、"
        f"{len(to_classify)} 个待分类 —— **共省下 {saved} 个候选的 LLM 分类**"
        f"（确定性去重 {exact_deduped} + 本轮内语义合并 {within_round}"
        f" + 库内已有 {len(to_merge)}）"
        f"｜灰区 LLM 调用 {gray_calls} 次（判定重复 {gray_merged} 次）"
        f"、耗时 {gray_secs:.1f}s"
        + (f"（其中 {gray_failed} 个调用失败，已按新条目处理）"
           if gray_failed else "")
    )

    # 4. ③【贵】LLM 相关性分类：**只对真正的新候选**
    #    候选与 NewsItem 是多对一（同指纹的条目已并成一个候选），
    #    所以要按对象身份映射回去，不能按下标。
    by_item = {id(c.item): c for c in to_classify}
    if to_classify:
        classified, classify_failed = classify_news([c.item for c in to_classify])
    else:
        # 整轮全是重复（比如刚跑过一轮）—— 一次 LLM 都不用调，这正是顺序调整的目的
        classified, classify_failed = [], []
    relevant = [r for r in classified if r["relevant"]]
    dropped = len(classified) - len(relevant)

    # 5. ④ 入库：先并入已有，再插新增，共用一次 commit
    #
    # 改动前是「每 50 条新增 commit + 索引一次」。现在整轮末尾一次做完 ——
    # 本轮内的一致性由 ② 的 numpy 自相似负责（不再依赖"写一部分才能看见"），
    # 也就没有了"批内不可见"那个洞（A3 的成因之一）。
    logger.info(f"[入库] 开始：{len(to_merge)} 个并入已有 + "
                f"{len(relevant)} 条新增（含 bge-m3 向量化 + 索引，请勿当作卡死）")
    _t_ingest = time.time()
    # 用 dict 按 id 去重：**两个候选可能并进同一条已有新闻**（它们的最近邻都指向它），
    # 而两次 `_merge_into` 都可能返回 True（第二次的正文更长）。
    # 重复 id 会让 Chroma 抛 DuplicateIDError，且**异常发生在 db.commit() 之后** ——
    # 整轮会以「数据已提交、索引没更新」的半完成状态收场。
    changed: dict[str, News] = {}
    for news_row, cand in to_merge:
        if _merge_into(db, news_row, cand):
            changed[news_row.id] = news_row

    new_news: list[News] = []
    for r in relevant:
        cand = by_item.get(id(r["item"]))
        if cand is None:              # 理论上不会发生；真要发生，宁可跳过也不要瞎插
            logger.warning("[入库] 分类结果找不到对应候选，已跳过该条")
            continue
        new_news.append(_insert_new(db, cand, r))

    db.commit()

    # 6. ⑤ 索引：整轮末尾一次性 upsert（新增的 + 被改写的）
    _upsert_index(new_news + list(changed.values()))
    # P3：只在**确有新增或被改写的条目**时重建 BM25。
    # 普通合并（只涨 source_count / source_urls）不影响 BM25 —— 它的 metadata
    # 只存 news_id，正文没变就没必要重建（全量重建实测 2.2s / 3400 条，
    # 而改动前是**每轮无条件**重建）。
    if new_news or changed:
        _rebuild_bm25(db)
    logger.info(f"[入库] 完成：新增 {len(new_news)}、合并 {len(to_merge)}、"
                f"改写重索引 {len(changed)}，耗时 {time.time() - _t_ingest:.0f}s")

    result = {
        "collected": len(raw),
        "candidates": len(cands),
        "exact_deduped": exact_deduped,
        "within_round_merged": within_round,
        "classified": len(classified),
        "relevant": len(relevant),
        # `dropped` **只表示「判为不相关」**，不含分类失败的条目 ——
        # 后者是 `classify_failed`。两者混在一起过，正是 2026-09-21 那次
        # 「丢了 1820 条却在日志里显示『丢弃 0』」的成因。
        # ⚠️ 2026-09-22 起口径又变了一次：重复条目不再进分类，所以它们
        # 既不在 `dropped` 也不在 `classify_failed`，而是计入 `merged`。
        "dropped": dropped,
        "classify_failed": len(classify_failed),
        "new": len(new_news),
        "merged": len(to_merge),
        "gray_check_failed": gray_failed,
    }
    logger.info(
        f"数据采集Agent: 采集{result['collected']} 候选{result['candidates']}"
        f"（确定性去重{exact_deduped} 本轮内合并{within_round}）"
        f" 分类成功{result['classified']} 相关{result['relevant']}"
        f" 丢弃{dropped}(判为不相关) 分类失败{result['classify_failed']}"
        f" 新增{result['new']} 合并{result['merged']}"
    )
    if result["classify_failed"]:
        logger.warning(
            f"⚠️ 本轮有 {result['classify_failed']} 条**未分类**（LLM 分类失败），"
            f"这批条目未入库；水位线将不推进，下一轮重取同一窗口"
        )
    if gray_failed:
        logger.warning(
            f"⚠️ 本轮有 {gray_failed} 个候选的**灰区判重**调用失败，已按新条目入库 "
            f"（fail-open：宁可多一条，也不误吞真新闻）。若持续出现请检查 LLM 可用性"
        )
    return result
