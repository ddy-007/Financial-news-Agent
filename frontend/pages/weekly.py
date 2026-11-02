"""页面：周度综述 (page_weekly)。"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from frontend.api_client import api_get, api_post
from frontend.constants import NEUTRAL
from frontend.utils import _fmt_score


# ================= 页面：周度综述 =================
def page_weekly():
    st.title("📅 周度综述")
    data = api_get("/api/v1/reports/weekly")
    if not data:
        st.info("暂无周报。周报在**每周最后一个交易日**收盘后自动生成（需本周至少 2 份日报）。")
        if st.button("手动生成本周周报"):
            resp = api_post("/api/v1/reports/weekly/generate")
            if resp and resp.get("id"):
                st.rerun()
        return

    c = data.get("content") or {}
    week_start = c.get("week_start") or ""
    week_end = c.get("week_end") or ""
    st.caption(
        f"所属自然周：{week_start} ~ {week_end}  ·  "
        f"纳入 {c.get('daily_count', 0)} 份日报  ·  非交易日不计入"
    )
    series = c.get("daily_series") or []
    included_days = {r.get("date") for r in series if isinstance(r, dict)}
    daily_reports = api_get("/api/v1/reports", {"limit": 10})
    missing_days = set()
    for report in daily_reports or []:
        day = report.get("report_day") or str(report.get("date") or "")[:10]
        if (week_start and week_end and week_start <= day <= week_end
                and day not in included_days):
            missing_days.add(day)
    if missing_days:
        st.warning(
            f"本周报尚未纳入后来补齐的日报：{'、'.join(sorted(missing_days))}。"
            f"以下内容仍基于原有 {c.get('daily_count', 0)} 份日报。"
        )

    c1, c2, c3 = st.columns(3)
    c1.metric("周定调", data.get("sentiment", "中性"))
    c2.metric("周综合分", _fmt_score(data.get("score")))
    div = data.get("divergence")
    c3.metric("日均分歧度", f"{div:.2f}" if div is not None else "—")

    # 日报定调走势（单序列，无需图例）
    st.markdown("#### 本周综述")
    st.write(c.get("market_summary") or data.get("title", ""))

    if series:
        st.markdown("#### 本周每日定调走势")
        sdf = pd.DataFrame(series)
        sdf["date"] = pd.to_datetime(sdf["date"])
        fig = px.line(sdf, x="date", y="score", markers=True)
        fig.update_traces(line_width=2, marker_size=8)
        fig.add_hline(y=0, line_width=1, line_color=NEUTRAL)
        fig.update_layout(
            xaxis_title=None, yaxis_title="综合情绪分",
            showlegend=False, hovermode="x unified",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True)

    for label, key in (
        ("本周主线驱动", "key_drivers"),
        ("板块机会", "sector_opportunities"),
        ("下周需警惕的风险", "risks"),
    ):
        items = c.get(key) or []
        with st.expander(f"{label}（{len(items)}）"):
            for x in items:
                st.markdown(f"- {x}")

    if c.get("consensus_note"):
        with st.expander("专家观点演变"):
            st.write(c["consensus_note"])

    st.warning("⚠️ 以上内容由 AI 基于历史数据生成，仅供参考，不构成投资建议。")
