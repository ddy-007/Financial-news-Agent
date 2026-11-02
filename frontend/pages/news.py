"""页面：新闻流 (page_news)。"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from frontend.api_client import api_get, api_post, get_api_base
from frontend.utils import _fmt_themes


# ================= 页面：新闻流 =================
def page_news():
    st.title("📰 金融新闻流")

    col1, col2 = st.columns([4, 1])
    keyword = col1.text_input("关键词（可空）", "")
    with col2:
        st.write("")
        if st.button("采集最新新闻", use_container_width=True):
            with st.spinner("正在采集并整理新闻，请稍候…"):
                resp = api_post("/api/v1/news/collect")
            if resp is not None:
                st.success("新闻采集完成，列表已刷新。")

    # ① 日期清单单独取 —— 它回答的是「库里有哪些日期」，与新闻总量无关（一条聚合查询）。
    #    原先是拿一批新闻（limit=1000）再从中"推"日期，数据量一超过上限，
    #    老日期连按钮都不会出现（实证：库里 4632 条覆盖 10 天，前端那 1000 条只剩 2 天）。
    dates_resp = api_get("/api/v1/news/dates")
    if dates_resp is None:
        # 请求失败——api_get 已弹出具体错误。别再往下走，
        # 否则会显示"暂无新闻"，把"后端没起"误导成"库是空的"。
        return
    date_rows = dates_resp.get("dates") or []
    if not date_rows:
        st.info("库里还没有新闻，点击「采集最新新闻」按钮。")
        return

    dates = [date.fromisoformat(r["date"]) for r in date_rows]   # 接口已按日期降序
    date_count = {date.fromisoformat(r["date"]): r["count"] for r in date_rows}
    _span = (dates[0] - dates[-1]).days + 1
    _missing = _span - len(dates)
    _suffix = f"，其中 **{_missing} 天无数据**）" if _missing > 0 else "）"
    st.caption(
        f"📅 库中新闻覆盖 **{dates[-1]} ~ {dates[0]}**"
        f"（共 {len(dates)} 天有数据" + _suffix
    )

    col_a, col_b = st.columns([2, 1])
    selected_date = col_a.date_input(
        "日期", value=dates[0], min_value=dates[-1],
        max_value=max(date.today(), dates[0]),
    )
    limit = col_b.selectbox("显示条数", [100, 300, 500, 1000], index=1)
    if selected_date is None:
        st.info("请选择日期。")
        return

    filter_key = (get_api_base(), selected_date, keyword, limit)
    if st.session_state.get("news_filter_key") != filter_key:
        st.session_state.news_filter_key = filter_key
        st.session_state.news_page = 1
    page = st.session_state.news_page
    offset = (page - 1) * limit

    params: dict = {
        "start": selected_date.isoformat(),
        "end": selected_date.isoformat(),
        "limit": limit,
        "offset": offset,
    }
    if keyword:
        params["keyword"] = keyword

    data = api_get("/api/v1/news", params)
    if data is None:
        return
    if not data:
        st.info("该日期或关键词下没有新闻数据。")
        if page > 1 and st.button("上一页", icon=":material/chevron_left:"):
            st.session_state.news_page -= 1
            st.rerun()
        return

    df = pd.DataFrame(data)
    total = date_count.get(selected_date) if not keyword else None
    if total is None and len(data) == limit:
        next_params = {**params, "limit": 1, "offset": offset + limit}
        has_more = bool(api_get("/api/v1/news", next_params))
    else:
        has_more = offset + len(data) < total if total is not None else False

    df["源数"] = df["source_count"].apply(
        lambda x: f"🔥 {int(x)}源" if x and x >= 2 else "1源"
    )
    df["题材"] = df["themes"].apply(_fmt_themes)
    df["时间"] = pd.to_datetime(df["publish_time"]).dt.strftime("%H:%M")

    display = df[
        ["时间", "title", "url", "category", "market", "源数", "题材"]
    ].copy()
    display.columns = ["时间", "标题", "原文", "分类", "市场", "源数", "题材"]
    display = display.reset_index(drop=True)
    display.insert(0, "序号", range(offset + 1, offset + len(display) + 1))

    st.dataframe(
        display,
        column_config={
            "序号": st.column_config.NumberColumn("序号", width=55),
            "原文": st.column_config.LinkColumn(
                "原文", display_text="🔗", width=60, help="打开原文"
            ),
        },
        hide_index=True,  # 隐藏 pandas 原生索引，只显示「序号」列
        use_container_width=True,
        height=600,
    )
    end = offset + len(display)
    count_label = f"第 {offset + 1}-{end} 条"
    if total is not None:
        count_label += f" / 当日共 {total} 条"
    st.caption(count_label)

    prev_col, page_col, next_col = st.columns([1, 2, 1])
    with prev_col:
        if st.button("上一页", icon=":material/chevron_left:",
                     disabled=page == 1, use_container_width=True):
            st.session_state.news_page -= 1
            st.rerun()
    page_col.caption(f"第 {page} 页")
    with next_col:
        if st.button("下一页", icon=":material/chevron_right:",
                     disabled=not has_more, use_container_width=True):
            st.session_state.news_page += 1
            st.rerun()
