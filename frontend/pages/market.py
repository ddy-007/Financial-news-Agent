"""页面：行情 (page_market)。"""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from frontend.api_client import api_get, api_post
from frontend.constants import DOWN_COLOR, NEUTRAL, UP_COLOR


# ================= 页面：行情 =================
def page_market():
    st.title("📉 指数行情")
    if st.button("采集最新行情"):
        with st.spinner("正在采集最新行情，请稍候…"):
            resp = api_post("/api/v1/market/collect")
        if resp is not None:
            st.success("行情采集完成，数据已刷新。")

    data = api_get("/api/v1/market", {"limit": 500})
    if not data:
        st.info("暂无行情数据，点击上方「采集最新行情」按钮。")
        _render_sector_board()   # 板块榜不依赖行情数据，行情为空也要显示
        return

    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])

    # 可选指数（多选），默认全选
    name_map = {r["symbol"]: r["name"] for r in data}
    opts = {f"{name}({sym})": sym for sym, name in name_map.items()}
    selected = st.multiselect(
        "选择指数（可多选）", list(opts.keys()), default=list(opts.keys())
    )

    if selected:
        syms = [opts[s] for s in selected]
        plot_df = df[df["symbol"].isin(syms)].sort_values("date")
        fig = px.line(
            plot_df,
            x="date",
            y="close",
            color="name",
            markers=False,
            title="指数收盘价走势",
        )
        fig.update_layout(
            xaxis_title=None,
            yaxis_title="收盘价",
            legend_title=None,
            hovermode="x unified",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True)

    # 最新行情表
    latest = df.sort_values("date").drop_duplicates("symbol", keep="last")
    latest["方向"] = latest["change_pct"].map(
        lambda v: "—" if pd.isna(v) else ("涨 ↑" if v > 0 else ("跌 ↓" if v < 0 else "平 →"))
    )
    table = latest[["name", "symbol", "close", "change_pct", "方向", "date"]]
    table.columns = ["名称", "代码", "收盘", "涨跌幅%", "方向", "日期"]
    st.dataframe(table, use_container_width=True)

    _render_sector_board()


def _render_sector_board():
    """板块涨跌榜。

    独立成函数是为了**不受行情数据影响** —— 原先是内联在 `page_market()` 里，
    而行情为空时那个函数会提前 return，导致板块有数据也看不到。
    """
    st.divider()
    st.subheader("🏭 板块涨跌榜")

    sec = api_get("/api/v1/sectors")
    if not sec or not sec.get("rows"):
        st.info("暂无板块数据。板块采集在工作日 17:40 进行 —— 后端没开的那几天不会采集。")
        return

    # 数据日期必须显著：板块采集只在工作日跑，后端一停就断档。
    # 只写个日期不够——人看到"09-16"不会立刻反应过来那是几天前。
    sec_date = sec["date"]
    days_ago = max(0, (date.today() - date.fromisoformat(sec_date)).days)
    if days_ago == 0:
        st.caption(f"数据日期：{sec_date}（今日）")
    else:
        st.warning(
            f"⚠️ **数据日期：{sec_date} —— {days_ago} 天前**，不是今日行情。"
            "板块采集仅在交易日 17:40 进行。"
        )

    df_sec = pd.DataFrame(sec["rows"]).dropna(subset=["change_pct"])
    if df_sec.empty:
        st.info("该日板块数据为空。")
        return

    # 只画头尾各 TOP_N 个：84 个板块全塞进去没法读（全市场常常只有十几个在涨）。
    # 板块数不足时**收窄窗口**，否则两侧会重叠——极端情况下"领跌"行里
    # 列出的其实是上涨板块，自相矛盾。
    TOP_N = 8
    ranked = df_sec.sort_values("change_pct", ascending=False)
    n = max(1, min(TOP_N, len(ranked) // 2)) if len(ranked) > 1 else 1
    show = pd.concat([ranked.head(n), ranked.tail(n)]).drop_duplicates("name")
    # 升序：plotly 把数据第一行画在最下面，故最大的涨落在最上方
    show = show.sort_values("change_pct")
    show["方向"] = show["change_pct"].apply(
        lambda v: "涨" if v > 0 else ("跌" if v < 0 else "平")
    )
    show["标签"] = show["change_pct"].map(lambda v: f"{v:+.2f}%")

    up_n = int((df_sec["change_pct"] > 0).sum())
    down_n = int((df_sec["change_pct"] < 0).sum())
    flat_n = len(df_sec) - up_n - down_n
    breadth = f"全市场 {len(df_sec)} 个板块：**{up_n} 涨 / {down_n} 跌**"
    if flat_n:                      # 不写会让"涨+跌 ≠ 总数"看着像算错了
        breadth += f" / {flat_n} 平"
    st.caption(breadth + f"（图中只列头尾各 {n} 个）")

    fig2 = px.bar(
        show, x="change_pct", y="name", orientation="h",
        color="方向",
        color_discrete_map={"涨": UP_COLOR, "跌": DOWN_COLOR, "平": NEUTRAL},
        text="标签",
    )
    # 条上直接标数值 —— 这同时是 CVD 对比度不足时必须的补偿（dataviz 技能硬性要求）
    fig2.update_traces(textposition="outside", cliponaxis=False)
    fig2.update_layout(
        xaxis_title="涨跌幅 %", yaxis_title=None, legend_title=None,
        height=max(320, 34 * len(show)),
        margin=dict(l=0, r=40, t=10, b=0),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        bargap=0.25,
    )
    fig2.add_vline(x=0, line_width=1, line_color=NEUTRAL)
    st.plotly_chart(fig2, use_container_width=True)
