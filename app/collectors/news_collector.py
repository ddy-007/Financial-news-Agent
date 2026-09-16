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

from app.retry import with_retry


@dataclass
class NewsItem:
    title: str
    content: str = ""
    source: str = ""
    url: str | None = None
    publish_time: datetime | None = None
    category: str | None = None


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


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
    def fetch(self) -> list[NewsItem]:
        ...

    @with_retry(retry_times=3, retry_label="新闻接口")
    def _get_json(self, url: str, **kwargs) -> dict:
        with httpx.Client(headers=_HEADERS, timeout=15, follow_redirects=True) as c:
            r = c.get(url, **kwargs)
            r.raise_for_status()
            return r.json()


class SinaCollector(BaseCollector):
    source_name = "新浪财经"
    # 新浪 7x24 财经快讯，按 page 翻页回溯
    API = ("https://zhibo.sina.com.cn/api/zhibo/feed"
           "?page={page}&page_size=50&zhibo_id=152&tag_id=0")

    def fetch(self) -> list[NewsItem]:
        from app.config import settings

        cutoff = datetime.now() - timedelta(days=settings.news_lookback_days)
        items = []
        for page in range(1, 21):  # 最多翻 20 页
            data = self._get_json(self.API.format(page=page))
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

    def fetch(self) -> list[NewsItem]:
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
            data = self._get_json(url)
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
    BASE = "https://www.cls.cn/api/cache"

    @staticmethod
    def _sign(params: dict) -> str:
        """财联社签名：参数排序 → SHA1 → MD5。私有接口，可能随版本变化。"""
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sha1 = hashlib.sha1(query.encode()).hexdigest()
        return hashlib.md5(sha1.encode()).hexdigest()

    def fetch(self) -> list[NewsItem]:
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
            data = self._get_json(self.BASE, params=params)
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


def collect_all_news() -> list[NewsItem]:
    """采集全部源，单源异常不影响其他源。"""
    all_items: list[NewsItem] = []
    for col in _COLLECTORS:
        try:
            all_items.extend(col.fetch())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{col.source_name}] 采集失败: {e}")
    return all_items
