"""新闻采集：新浪财经 / 东方财富 / 财联社。

每个源一个 Collector，统一返回 NewsItem；单源失败不影响其他源。
注意：部分接口可能因源站调整而失效，均做了 try/except 降级。
"""
from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from loguru import logger

from app.collectors.http_client import get_client
from app.retry import with_retry


@dataclass
class NewsItem:
    title: str
    content: str = ""
    source: str = ""
    url: str | None = None
    publish_time: datetime | None = None
    category: str | None = None


@dataclass
class Anchor:
    """某源上一轮的水位线（`collector_state` 表的一行）。"""
    last_ts: datetime | None = None
    last_id: str | None = None


def _cutoff(anchor: Anchor | None) -> datetime:
    """本轮的停止条件 —— **增量规则唯一的落地处**。

    > 有水位线：`last_ts - news_overlap_minutes`
    > 无水位线（首次运行 / 水位线丢失）：`now - news_lookback_days`

    两个要点：

    1. **留重叠窗口是故意的**：防「上一轮采集结束」到「这一轮开始」之间因调度抖动、
       时钟偏差、源站补录而漏掉一小段。冗余一点，代价只是多抓几分钟的量。
    2. **锚点一律用源站发布时间**，不用本地 `now()` —— 否则源站时间与本地时钟
       有偏差时，水位线会系统性偏移。

    只写这一处：三个源若各算各的，迟早会漂移成三套规则。
    """
    from app.config import settings

    if anchor is not None and anchor.last_ts is not None:
        return anchor.last_ts - timedelta(minutes=settings.news_overlap_minutes)
    return datetime.now() - timedelta(days=settings.news_lookback_days)


@dataclass
class RejectedItem:
    """边界校验不合格的记录。**只用于记日志**，不建表（见设计 §4.2）。"""
    source: str
    reason: str                    # "empty_title" | "bad_publish_time" | ...
    payload: dict                  # 原始记录
    title: str = ""
    url: str | None = None
    publish_time: datetime | None = None


@dataclass
class SourceResult:
    """单个源一轮采集的结果。

    有了它，`collect_all_news` 才能如实报告「哪个源失败了、失败了几页、有没有被截断」——
    改动前它只把三源条目**混在一个列表**里返回，源级信息全部丢失。
    """
    source: str
    items: list[NewsItem]
    rejected: list[RejectedItem]   # 边界校验不合格的条目（只用于记日志）
    ok: bool                       # **本轮是否完整取到**：无失败页才算 True（见 fetch 内注释）
    failed_pages: int = 0          # 本轮失败的页数（逐页容错，已得的页保留）
    truncated: bool = False        # 是否撞页上限（true 时更旧的新闻本轮没取到）


def _ts_to_dt(ts) -> datetime | None:
    """时间戳转 datetime（兼容秒 / 毫秒）。"""
    try:
        ts = int(ts)
        if ts > 10_000_000_000:  # 13 位为毫秒，转秒
            ts = ts // 1000
        return datetime.fromtimestamp(ts)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _parse_dt(v) -> datetime | None:
    """解析日期时间：兼容 'YYYY-MM-DD HH:MM:SS' 字符串与秒/毫秒时间戳。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return _ts_to_dt(v)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return _ts_to_dt(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# 页面型源（新浪）连续失败多少页就放弃本轮。
#
# 实测（2026-09-22）：源整体挂掉时，「逐页容错 + 继续下一页」会让 20 页各重试 3 次
# = **57 次请求 / 55 秒**，纯属白跑。3 页足够区分「偶发单页抖动」（重试后多半就成功了）
# 与「源整体不可用」。
MAX_CONSECUTIVE_PAGE_FAILS = 3


def _validate(source: str, item: NewsItem, payload: dict) -> RejectedItem | None:
    """边界校验。合格返回 None，不合格返回 `RejectedItem`（**不进 items，只记日志**）。

    三条规则（设计 §9.4）：

    1. `title` 去空白后**非空** —— 空标题会一路进 LLM 分类 prompt，是纯噪音；
    2. `publish_time` **可解析** —— 不可解析时**不回退为 `now()`**。
       回退看似"更宽容"，实则会把一条来历不明的时间当成真实发布时间写进库，
       再被水位线、时效判断当成事实依据 —— 错得比丢弃更隐蔽；
    3. `url` 去空白，空串归一为 `None`（下游 `_item_to_news` 靠 None 避开唯一约束冲突）。
    """
    if not (item.title or "").strip():
        return RejectedItem(source=source, reason="empty_title", payload=payload,
                            title=item.title, url=item.url)
    if item.publish_time is None:
        return RejectedItem(source=source, reason="bad_publish_time", payload=payload,
                            title=item.title, url=item.url)
    item.title = item.title.strip()
    if item.url is not None and not item.url.strip():
        item.url = None
    return None


def _log_rejected(r: RejectedItem) -> None:
    """脏记录落点：**一行一条 WARNING，不建表**（设计 §4.2 / R-2）。

    只记 title / url / reason 与原始记录摘要 —— 原始 payload 通常含完整的
    `title/content/brief`，全量打出来会把日志淹掉，摘 120 字足够定位。
    """
    logger.warning(
        f"[{r.source}] 丢弃记录 reason={r.reason} "
        f"title={str(r.title)[:40]!r} url={r.url!r} "
        f"payload={str(r.payload)[:120]}"
    )


class BaseCollector(ABC):
    source_name: str = ""

    @abstractmethod
    def fetch(self, client: httpx.Client | None = None,
              anchor: Anchor | None = None) -> SourceResult:
        """采集本源的条目。

        **返回 `SourceResult` 而不是裸列表**（2026-09-22 P1 起）：调用方需要知道
        「这个源成功了吗 / 失败了几页 / 有没有撞页上限 / 丢了哪些脏记录」——
        裸列表这些都无从表达，源级信息会在 `collect_all_news` 处被抹平。
        """
        ...

    @with_retry(retry_times=3, retry_label="新闻接口")
    def _get_json(self, url: str, client: httpx.Client | None = None,
                  **kwargs) -> dict:
        """`client` 为 None 时取共享单例。

        允许注入是为了**构造测试**：给一个返回固定 JSON 的假 client，
        就能离线验证四道 guard 与逐页容错，不必打真实源。
        """
        c = client or get_client()
        r = c.get(url, **kwargs)
        r.raise_for_status()
        return r.json()


class SinaCollector(BaseCollector):
    source_name = "新浪财经"
    # 新浪 7x24 财经快讯，按 page 翻页回溯
    API = ("https://zhibo.sina.com.cn/api/zhibo/feed"
           "?page={page}&page_size=50&zhibo_id=152&tag_id=0")

    def fetch(self, client: httpx.Client | None = None,
              anchor: Anchor | None = None) -> SourceResult:
        from app.config import settings

        cutoff = _cutoff(anchor)
        max_pages = max(1, int(settings.news_max_pages))
        items: list[NewsItem] = []
        rejected: list[RejectedItem] = []
        seen: set[str] = set()
        failed_pages = 0
        truncated = False
        consecutive_fails = 0

        for page in range(1, max_pages + 1):
            try:
                data = self._get_json(self.API.format(page=page), client=client)
            except Exception as e:  # noqa: BLE001
                # 逐页容错：本页失败**不丢已得的页**，继续下一页（设计 §9.2）
                failed_pages += 1
                consecutive_fails += 1
                logger.warning(f"[{self.source_name}] 第 {page} 页失败，跳过：{e}")
                if consecutive_fails >= MAX_CONSECUTIVE_PAGE_FAILS:
                    logger.error(
                        f"[{self.source_name}] 连续 {consecutive_fails} 页失败，"
                        f"判定本源整体不可用，提前停止（已得的 {len(items)} 条保留）"
                    )
                    break
                continue
            consecutive_fails = 0

            feed = (data.get("result") or {}).get("data") or {}
            lst = (feed.get("feed") or {}).get("list") or []
            if not lst:                          # guard ① 空页 → 正常结束
                break

            for row in lst:
                rich = row.get("rich_text") or ""
                title = rich
                m = re.match(r"【(.+?)】", rich)  # 【标题】正文 格式
                if m:
                    title = m.group(1)
                # 新浪无稳定 id 字段，用 docurl 作键；没有 docurl 时退化为「时间+正文前缀」。
                key = str(row.get("docurl") or "") or f"{row.get('create_time')}|{rich[:60]}"
                if key in seen:                  # guard ② 稳定 id 去重
                    continue
                seen.add(key)
                item = NewsItem(
                    title=title,
                    content=rich,
                    source=self.source_name,
                    url=row.get("docurl"),
                    publish_time=_parse_dt(row.get("create_time")),
                    category="快讯",
                )
                bad = _validate(self.source_name, item, row)
                if bad:
                    rejected.append(bad)
                else:
                    items.append(item)

            last_dt = _parse_dt(lst[-1].get("create_time"))
            if last_dt and last_dt < cutoff:
                break
            if page == max_pages:                # guard ④ 跑满上限 → 截断（不抛错）
                truncated = True

        for r in rejected:
            _log_rejected(r)
        # ok 的判据是「**没有任何一页失败**」，不是"跑完了就算成功"：
        # 页是新→旧顺序的，中间某页失败会在水位线里留一个**中段空洞**，
        # 而水印一旦推进，下一轮只会取最新那一段 —— 空洞永不回补。
        ok = failed_pages == 0
        logger.info(f"[{self.source_name}] 采集 {len(items)} 条"
                    f"{f'（失败 {failed_pages} 页）' if failed_pages else ''}"
                    f"{'（⚠️ 触页上限，已截断）' if truncated else ''}")
        return SourceResult(source=self.source_name, items=items, rejected=rejected,
                            ok=ok, failed_pages=failed_pages, truncated=truncated)


class EastmoneyCollector(BaseCollector):
    source_name = "东方财富"
    COLUMN = 102  # 全球财经快讯栏目

    def fetch(self, client: httpx.Client | None = None,
              anchor: Anchor | None = None) -> SourceResult:
        from app.config import settings

        cutoff = _cutoff(anchor)
        max_pages = max(1, int(settings.news_max_pages))
        items: list[NewsItem] = []
        rejected: list[RejectedItem] = []
        seen: set[str] = set()
        failed_pages = 0
        truncated = False
        sort_end = ""

        for page in range(1, max_pages + 1):
            ts = int(time.time() * 1000)
            url = (
                "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
                f"?client=web&biz=web_724&fastColumn={self.COLUMN}"
                f"&sortEnd={sort_end}&pageSize=50&req_trace={ts}"
            )
            try:
                data = self._get_json(url, client=client)
            except Exception as e:  # noqa: BLE001
                # ⚠️ 游标型分页**不能**「继续下一页」：本页失败 ⇒ 游标没推进，
                # 下一次请求会拿**同一个游标**再打一遍 —— 那不是翻页，是空转。
                # （设计 §9.2 的「继续下一页」只对页面型（新浪 `page=N`）成立，
                #  2026-09-22 实测发现并写回文档。）
                failed_pages += 1
                logger.warning(
                    f"[{self.source_name}] 第 {page} 页失败：{e}；"
                    f"游标型分页跳不过本页（游标未推进），在此停止"
                    f"（已得的 {len(items)} 条保留）"
                )
                break

            d = data.get("data") or {}
            rows = d.get("fastNewsList") or []
            if not rows:                          # guard ① 空页 → 正常结束
                break

            for row in rows:
                code = row.get("code")
                if code and str(code) in seen:    # guard ② 稳定 id（code）去重
                    continue
                if code:
                    seen.add(str(code))
                item = NewsItem(
                    title=row.get("title") or row.get("summary") or "",
                    content=row.get("summary") or "",
                    source=self.source_name,
                    url=f"https://finance.eastmoney.com/a/{code}.html" if code else None,
                    publish_time=_parse_dt(row.get("showTime")),
                    category="财经",
                )
                bad = _validate(self.source_name, item, row)
                if bad:
                    rejected.append(bad)
                else:
                    items.append(item)

            new_cursor = d.get("sortEnd") or ""
            if new_cursor == sort_end:            # guard ③ 游标没前进 → 再翻也是同一批
                logger.warning(f"[{self.source_name}] 游标未前进（{sort_end!r}），"
                               f"第 {page} 页后停止翻页")
                break
            sort_end = new_cursor

            last_dt = _parse_dt(rows[-1].get("showTime"))
            if last_dt and last_dt < cutoff:
                break
            if page == max_pages:                 # guard ④ 跑满上限 → 截断（不抛错）
                truncated = True

        for r in rejected:
            _log_rejected(r)
        # 同新浪：任何一页失败 → ok=False（中间空洞不可回补，见 SinaCollector.fetch）
        ok = failed_pages == 0
        logger.info(f"[{self.source_name}] 采集 {len(items)} 条"
                    f"{f'（失败 {failed_pages} 页）' if failed_pages else ''}"
                    f"{'（⚠️ 触页上限，已截断）' if truncated else ''}")
        return SourceResult(source=self.source_name, items=items, rejected=rejected,
                            ok=ok, failed_pages=failed_pages, truncated=truncated)


class ClsCollector(BaseCollector):
    source_name = "财联社"
    # ⚠️ 端点必须是 v1/roll/get_roll_list。
    # 曾经用的是 `api/cache`，那个端点的 `last_time` 游标**完全不生效**——
    # 翻 20 页拿回的是同一批 20 条，每轮采集实际只贡献 20 条唯一内容
    # （库里财联社长期只有 123 条，而新浪 3473、东财 987，就是这个原因）。
    # 2026-09-20 实测：该端点连续请求两次 id 完全相同；换成 v1 端点后
    # 连续 10 页取得 200 条、**全部唯一**且时间稳定倒推。
    BASE = "https://www.cls.cn/v1/roll/get_roll_list"

    @staticmethod
    def _sign(params: dict) -> str:
        """财联社签名：参数排序 → SHA1 → MD5。私有接口，可能随版本变化。"""
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sha1 = hashlib.sha1(query.encode()).hexdigest()
        return hashlib.md5(sha1.encode()).hexdigest()

    def fetch(self, client: httpx.Client | None = None,
              anchor: Anchor | None = None) -> SourceResult:
        from app.config import settings

        cutoff = _cutoff(anchor)
        max_pages = max(1, int(settings.news_max_pages))
        items: list[NewsItem] = []
        rejected: list[RejectedItem] = []
        seen: set[str] = set()
        failed_pages = 0
        truncated = False
        last_time = 0

        for page in range(1, max_pages + 1):
            params = {
                "app": "CailianpressWeb", "os": "web", "sv": "8.7.9",
                "name": "telegraph", "refresh_type": "1", "rn": "20",
                "last_time": str(last_time),
            }
            params["sign"] = self._sign(params)
            try:
                data = self._get_json(self.BASE, client=client, params=params)
            except Exception as e:  # noqa: BLE001
                # 同东财：游标型分页跳不过失败页（游标未推进），在此停止。
                failed_pages += 1
                logger.warning(
                    f"[{self.source_name}] 第 {page} 页失败：{e}；"
                    f"游标型分页跳不过本页（游标未推进），在此停止"
                    f"（已得的 {len(items)} 条保留）"
                )
                break

            rows = (data.get("data") or {}).get("roll_data") or []
            if not rows:                          # guard ① 空页 → 正常结束
                break

            for row in rows:
                rid = row.get("id")
                if rid is not None and str(rid) in seen:   # guard ② 稳定 id（id）去重
                    continue
                if rid is not None:
                    seen.add(str(rid))
                item = NewsItem(
                    title=row.get("title") or row.get("brief") or "",
                    content=row.get("brief") or row.get("content") or "",
                    source=self.source_name,
                    url=f"https://www.cls.cn/detail/{rid}" if rid else None,
                    publish_time=_ts_to_dt(row.get("ctime")),
                    category="快讯",
                )
                bad = _validate(self.source_name, item, row)
                if bad:
                    rejected.append(bad)
                else:
                    items.append(item)

            new_last = rows[-1].get("ctime") or 0
            if new_last == last_time:             # guard ③ 游标没前进 → 再翻也是同一批
                logger.warning(f"[{self.source_name}] 游标未前进（{last_time}），"
                               f"第 {page} 页后停止翻页")
                break
            last_time = new_last

            last_dt = _ts_to_dt(last_time)
            if last_dt and last_dt < cutoff:
                break
            if page == max_pages:                 # guard ④ 跑满上限 → 截断（不抛错）
                truncated = True

        for r in rejected:
            _log_rejected(r)
        # 同新浪：任何一页失败 → ok=False（中间空洞不可回补，见 SinaCollector.fetch）
        ok = failed_pages == 0
        logger.info(f"[{self.source_name}] 采集 {len(items)} 条"
                    f"{f'（失败 {failed_pages} 页）' if failed_pages else ''}"
                    f"{'（⚠️ 触页上限，已截断）' if truncated else ''}")
        return SourceResult(source=self.source_name, items=items, rejected=rejected,
                            ok=ok, failed_pages=failed_pages, truncated=truncated)


_COLLECTORS: list[BaseCollector] = [
    SinaCollector(),
    EastmoneyCollector(),
    ClsCollector(),
]


def collect_all_news(anchors: dict[str, Anchor] | None = None
                     ) -> list[SourceResult]:
    """采集全部源，单源异常不影响其他源。

    `anchors`：`{源名: Anchor}`，即各源上一轮的水位线。**为 None 或某源缺锚点时，
    该源退化为「距现在 `news_lookback_days` 天」** —— 首次运行、水位线丢失、
    或新增源都会走这条回退路径，所以它必须一直可用。

    ⚠️ **返回结构 2026-09-22 变了**：原为 `list[NewsItem]`（三源条目混在一个列表里），
    现按源分组返回 —— 下游要**按源**推进水位线、按源报告失败与截断，
    混在一起的信息不足以支撑这些判断。
    取全部条目：`[it for r in results for it in r.items]`。

    某个源失败时**仍返回该源的 `SourceResult`**（`ok=False`、`items=[]`），
    而不是把它从列表里省掉 —— 省掉的话下游无法区分「这个源没配」与「这个源本轮挂了」。
    """
    anchors = anchors or {}
    results: list[SourceResult] = []
    for col in _COLLECTORS:
        try:
            results.append(col.fetch(anchor=anchors.get(col.source_name)))
        except Exception as e:  # noqa: BLE001
            # P1 起 fetch 内部已逐页容错，能走到这里的都是**帧级**异常
            # （比如解析逻辑本身出错）。仍然不让它拖垮其他源。
            logger.warning(f"[{col.source_name}] 采集失败: {e}")
            results.append(SourceResult(source=col.source_name, items=[],
                                        rejected=[], ok=False))
    return results
