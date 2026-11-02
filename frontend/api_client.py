"""API 调用封装：GET / POST / 缓存管理。"""
from __future__ import annotations

import logging

import httpx
import streamlit as st

logger = logging.getLogger(__name__)


def get_api_base() -> str:
    return st.session_state.get("api_base", "http://localhost:8000")


@st.cache_resource
def _http_client() -> httpx.Client:
    return httpx.Client()


# ---- 按数据类型区分 TTL ----
# 行情/市场数据：变动频繁，30-60 秒
# 报告数据：生成成本高、变动少，300 秒（5 分钟）
# 新闻日期列表：轻量聚合，60 秒

@st.cache_data(ttl=60, show_spinner=False)
def _cached_get_default(api_base: str, path: str, params_items: tuple):
    """默认缓存：TTL 60 秒，适用于一般接口。"""
    params = dict(params_items)
    r = _http_client().get(f"{api_base}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=30, show_spinner=False)
def _cached_get_market(api_base: str, path: str, params_items: tuple):
    """行情/市场数据缓存：TTL 30 秒。"""
    params = dict(params_items)
    r = _http_client().get(f"{api_base}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300, show_spinner=False)
def _cached_get_report(api_base: str, path: str, params_items: tuple):
    """报告数据缓存：TTL 300 秒（5 分钟）。"""
    params = dict(params_items)
    r = _http_client().get(f"{api_base}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# 路径 → 缓存函数 的映射
_MARKET_PREFIXES = ("/api/v1/market", "/api/v1/sectors")
_REPORT_PREFIXES = ("/api/v1/reports",)


def _select_cache_fn(path: str):
    """根据 API 路径选择对应 TTL 的缓存函数。"""
    if any(path.startswith(p) for p in _MARKET_PREFIXES):
        return _cached_get_market
    if any(path.startswith(p) for p in _REPORT_PREFIXES):
        return _cached_get_report
    return _cached_get_default


def api_get(path: str, params: dict | None = None):
    try:
        params_items = tuple(sorted((params or {}).items()))
        cache_fn = _select_cache_fn(path)
        return cache_fn(get_api_base(), path, params_items)
    except httpx.HTTPStatusError as e:
        logger.exception("GET 请求失败（HTTP %s） path=%s", e.response.status_code, path)
        st.error(f"请求失败（HTTP {e.response.status_code}），请检查后端服务后重试。")
        return None
    except httpx.ConnectError:
        logger.exception("GET 请求失败（连接被拒绝） path=%s", path)
        st.error("无法连接后端服务，请确认后端已启动。")
        return None
    except httpx.TimeoutException:
        logger.exception("GET 请求超时 path=%s", path)
        st.error("请求超时，请检查网络或后端服务。")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("GET 请求失败（未知错误） path=%s", path)
        st.error("暂时无法获取数据，请检查后端服务后重试。")
        return None


def api_post(path: str, json_data: dict | None = None, *, invalidate_cache: bool = True):
    try:
        r = _http_client().post(f"{get_api_base()}{path}", json=json_data, timeout=180)
        r.raise_for_status()
        if invalidate_cache:
            _cached_get_default.clear()
            _cached_get_market.clear()
            _cached_get_report.clear()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.exception("POST 请求失败（HTTP %s） path=%s", e.response.status_code, path)
        st.error(f"操作失败（HTTP {e.response.status_code}），请检查后端服务后重试。")
        return None
    except httpx.ConnectError:
        logger.exception("POST 请求失败（连接被拒绝） path=%s", path)
        st.error("无法连接后端服务，请确认后端已启动。")
        return None
    except httpx.TimeoutException:
        logger.exception("POST 请求超时 path=%s", path)
        st.error("操作超时，请稍后重试。")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("POST 请求失败（未知错误） path=%s", path)
        st.error("操作暂时失败，请检查后端服务后重试。")
        return None


def clear_all_caches():
    """清除所有 API 缓存，供外部（如侧边栏刷新按钮）调用。"""
    _cached_get_default.clear()
    _cached_get_market.clear()
    _cached_get_report.clear()
