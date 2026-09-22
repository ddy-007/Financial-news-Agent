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
    rejected: list[RejectedItem]   # P0 恒为空，P1 起填充
    ok: bool                       # 该源本轮是否成功（fetch 未抛异常）
    failed_pages: int = 0          # P0 恒为 0，P1 起填充
    truncated: bool = False        # P0 恒为 False，P1 起填充


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


class BaseCollector(ABC):
    source_name: str = ""

    @abstractmethod
    def fetch(self, client: httpx.Client | None = None) -> list[NewsItem]:
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

    def fetch(self, client: httpx.Client | None = None) -> list[NewsItem]:
        from app.config import settings

        cutoff = datetime.now() - timedelta(days=settings.news_lookback_days)
        items = []
        for page in range(1, 21):  # 最多翻 20 页
            data = self._get_json(self.API.format(page=page), client=client)
            feed = (data.get("result") or {}).get("data") or {}
            lst = (feed.get("feed") or {}).get("list") or []
            if not lst:
                break
            for row in lst:
                rich = row.get("rich_text") or ""
                title = rich
                m = re.match(r"【(.+?)】", rich)  # 【标题】正文 格式
                if m:
                    title = m.group(1)
                items.append(NewsItem(
                    title=title,
                    content=rich,
                    source=self.source_name,
                    url=row.get("docurl"),
                    publish_time=_parse_dt(row.get("create_time")),
                    category="快讯",
                ))
            last_dt = _parse_dt(lst[-1].get("create_time"))
            if last_dt and last_dt < cutoff:
                break
        logger.info(f"[{self.source_name}] 采集 {len(items)} 条")
        return items


class EastmoneyCollector(BaseCollector):
    source_name = "东方财富"
    COLUMN = 102  # 全球财经快讯栏目

    def fetch(self, client: httpx.Client | None = None) -> list[NewsItem]:
        from app.config import settings

        cutoff = datetime.now() - timedelta(days=settings.news_lookback_days)
        items = []
        sort_end = ""
        for _ in range(20):  # 最多翻 20 页
            ts = int(time.time() * 1000)
            url = (
                "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
                f"?client=web&biz=web_724&fastColumn={self.COLUMN}"
                f"&sortEnd={sort_end}&pageSize=50&req_trace={ts}"
            )
            data = self._get_json(url, client=client)
            d = data.get("data") or {}
            rows = d.get("fastNewsList") or []
            if not rows:
                break
            for row in rows:
                code = row.get("code")
                items.append(NewsItem(
                    title=row.get("title") or row.get("summary") or "",
                    content=row.get("summary") or "",
                    source=self.source_name,
                    url=f"https://finance.eastmoney.com/a/{code}.html" if code else None,
                    publish_time=_parse_dt(row.get("showTime")),
                    category="财经",
                ))
            sort_end = d.get("sortEnd") or ""
            last_dt = _parse_dt(rows[-1].get("showTime"))
            if last_dt and last_dt < cutoff:
                break
        logger.info(f"[{self.source_name}] 采集 {len(items)} 条")
        return items


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

    def fetch(self, client: httpx.Client | None = None) -> list[NewsItem]:
        from app.config import settings

        cutoff = datetime.now() - timedelta(days=settings.news_lookback_days)
        items = []
        last_time = 0
        for _ in range(20):  # 最多翻 20 页
            params = {
                "app": "CailianpressWeb", "os": "web", "sv": "8.7.9",
                "name": "telegraph", "refresh_type": "1", "rn": "20",
                "last_time": str(last_time),
            }
            params["sign"] = self._sign(params)
            data = self._get_json(self.BASE, client=client, params=params)
            rows = (data.get("data") or {}).get("roll_data") or []
            if not rows:
                break
            for row in rows:
                items.append(NewsItem(
                    title=row.get("title") or row.get("brief") or "",
                    content=row.get("brief") or row.get("content") or "",
                    source=self.source_name,
                    url=f"https://www.cls.cn/detail/{row.get('id')}" if row.get("id") else None,
                    publish_time=_ts_to_dt(row.get("ctime")),
                    category="快讯",
                ))
            last_time = rows[-1].get("ctime") or 0
            last_dt = _ts_to_dt(last_time)
            if last_dt and last_dt < cutoff:
                break
        logger.info(f"[{self.source_name}] 采集 {len(items)} 条")
        return items


_COLLECTORS: list[BaseCollector] = [
    SinaCollector(),
    EastmoneyCollector(),
    ClsCollector(),
]


def collect_all_news() -> list[SourceResult]:
    """采集全部源，单源异常不影响其他源。

    ⚠️ **返回结构 2026-09-22 变了**：原为 `list[NewsItem]`（三源条目混在一个列表里），
    现按源分组返回 —— 下游要**按源**推进水位线、按源报告失败与截断，
    混在一起的信息不足以支撑这些判断。
    取全部条目：`[it for r in results for it in r.items]`。

    某个源失败时**仍返回该源的 `SourceResult`**（`ok=False`、`items=[]`），
    而不是把它从列表里省掉 —— 省掉的话下游无法区分「这个源没配」与「这个源本轮挂了」。
    """
    results: list[SourceResult] = []
    for col in _COLLECTORS:
        try:
            items = col.fetch()
            results.append(SourceResult(source=col.source_name, items=items,
                                        rejected=[], ok=True))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{col.source_name}] 采集失败: {e}")
            results.append(SourceResult(source=col.source_name, items=[],
                                        rejected=[], ok=False))
    return results
