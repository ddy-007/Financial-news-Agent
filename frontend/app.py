"""Streamlit 前端看板入口：侧边栏导航 + 页面分发。

原 streamlit_app.py 已拆分为：
  - constants.py     颜色常量
  - utils.py         格式化工具函数
  - api_client.py    API 调用封装（缓存、异常处理）
  - pages/           各页面模块
本文件仅负责全局配置、侧边栏和页面路由。
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Streamlit 只保证脚本目录可导入；显式加入项目根，避免 app/pages 同名模块冲突。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frontend.api_client import clear_all_caches, get_api_base
from frontend.pages.chat import page_chat
from frontend.pages.dashboard import page_dashboard
from frontend.pages.history import page_history
from frontend.pages.market import page_market
from frontend.pages.news import page_news
from frontend.pages.weekly import page_weekly


# ================= 主入口 =================
def main():
    st.set_page_config(page_title="金融新闻情报简报 Agent", page_icon="📈", layout="wide")
    st.sidebar.title("📈 金融情报简报 Agent")
    api_base = st.sidebar.text_input("后端地址", value=get_api_base())
    st.session_state.api_base = api_base
    if st.sidebar.button("刷新数据"):
        clear_all_caches()
        st.rerun()

    # 显式注册可调用页面，关闭 pages/ 目录自动发现，避免两套导航及空白模块页。
    page = st.navigation([
        st.Page(page_dashboard, title="每日研判", default=True),
        st.Page(page_weekly, title="周度综述"),
        st.Page(page_news, title="新闻流"),
        st.Page(page_market, title="行情"),
        st.Page(page_chat, title="AI 问答"),
        st.Page(page_history, title="历史与回测"),
    ])
    page.run()


if __name__ == "__main__":
    main()
