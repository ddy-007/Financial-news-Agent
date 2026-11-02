"""页面：历史与回测 (page_history)。"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.api_client import api_get
from frontend.pages.dashboard import page_dashboard


# ================= 页面：历史与回测 =================
def page_history():
    st.title("📚 历史研判与回测")
    backtest = api_get("/api/v1/reports/backtest")
    if backtest:
        c1, c2, c3 = st.columns(3)
        c1.metric("回测样本数", backtest.get("total", 0))
        c2.metric("方向正确数", backtest.get("correct", 0))
        acc = backtest.get("accuracy", 0)
        c3.metric("方向准确率", f"{acc}%")
        neutral_band = backtest.get("score_neutral_band")
        if isinstance(neutral_band, (int, float)):
            rule = (
                f"综合情绪分 >{neutral_band:g} 视为看多、"
                f"<-{neutral_band:g} 视为看空"
            )
        else:
            rule = "看多/看空阈值由后端配置决定"
        st.caption(
            f"回测规则：{rule}，与上证指数次日涨跌方向比对。仅供参考。"
        )
        by_bucket = backtest.get("by_bucket", [])
        if by_bucket:
            st.markdown("#### 按情绪分档胜率")
            bdf = pd.DataFrame(by_bucket)
            bdf.columns = ["情绪分档", "样本数", "正确数", "胜率%"]
            st.dataframe(bdf, use_container_width=True)
        details = backtest.get("details", [])
        if details:
            st.dataframe(pd.DataFrame(details), use_container_width=True)

    st.markdown("#### 历史报告")
    reports = api_get("/api/v1/reports", {"limit": 50})
    if reports:
        df = pd.DataFrame(reports)
        if "report_day" not in df:
            df["report_day"] = None
        df["业务日期"] = df["report_day"].fillna(df["date"].str[:10])
        display = df[["业务日期", "sentiment", "score", "title"]].copy()
        display.columns = ["业务日期", "情绪", "综合分", "标题"]
        st.dataframe(display, use_container_width=True, height=400)

        report_by_id = {r["id"]: r for r in reports if r.get("id")}
        selected_id = st.selectbox(
            "查看完整日报",
            [""] + list(report_by_id),
            format_func=lambda rid: (
                f"{report_by_id[rid].get('report_day') or report_by_id[rid]['date'][:10]}"
                f" · {report_by_id[rid].get('title', '')[:40]}"
            ) if rid else "选择日期",
        )
        if selected_id:
            page_dashboard(report_by_id[selected_id], include_market=False)
