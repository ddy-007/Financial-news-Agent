"""数据采集层：新闻 + 行情。"""
from app.collectors.market_collector import collect_all_market_data
from app.collectors.news_collector import NewsItem, collect_all_news

__all__ = ["NewsItem", "collect_all_news", "collect_all_market_data"]
