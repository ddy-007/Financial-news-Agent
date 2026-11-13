"""Metadata for news actually returned by the chat tools."""
from datetime import datetime
from urllib.parse import urlsplit

from langchain_core.messages import ToolMessage
from pydantic import BaseModel

_NEWS_TOOLS = {"search_news", "get_recent_news"}
_MAX_SOURCES = 10


class ChatSource(BaseModel):
    """Fields from a stored news record, which can represent a merged event."""

    id: str
    title: str
    published_at: str | None = None
    url: str | None = None


def news_source(news_id, title, published_at, url) -> ChatSource | None:
    if not isinstance(news_id, str) or not news_id.strip():
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if isinstance(published_at, datetime):
        published_at = published_at.isoformat()
    return ChatSource(
        id=news_id.strip(),
        title=title.strip(),
        published_at=published_at if isinstance(published_at, str) and published_at else None,
        url=_http_link(url),
    )


def _http_link(url) -> str | None:
    if not isinstance(url, str) or not url or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            return url
    except ValueError:
        pass
    return None


def collect_chat_sources(messages) -> list[ChatSource]:
    sources = {}
    for message in messages:
        if (not isinstance(message, ToolMessage) or message.name not in _NEWS_TOOLS
                or message.status != "success" or not isinstance(message.artifact, list)):
            continue
        for source in message.artifact:
            if isinstance(source, ChatSource):
                sources.setdefault(source.id, source)
                if len(sources) == _MAX_SOURCES:
                    return list(sources.values())
    return list(sources.values())
