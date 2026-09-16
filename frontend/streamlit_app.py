"""Streamlit 前端看板：每日研判 / 新闻流 / 行情 / AI 问答 / 历史回测。"""
from __future__ import annotations

import json

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

# ---- 配色（中国金融惯例：红涨绿跌；同时用 +/- 符号做次要编码，不单靠颜色）----
UP_COLOR = "#d03b3b"    # 涨（红）
DOWN_COLOR = "#0ca30c"  # 跌（绿）
NEUTRAL = "#898781"     # 中性/文字

st.set_page_config(page_title="金融新闻股市预测 Agent", page_icon="📈", layout="wide")


# ================= 基础工具 =================
def get_api_base() -> str:
    return st.session_state.get("api_base", "http://localhost:8000")


def api_get(path: str, params: dict | None = None):
    try:
        r = httpx.get(f"{get_api_base()}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        st.error(f"后端请求失败：{e}")
        return None


def api_post(path: str, json_data: dict | None = None):
    try:
        r = httpx.post(f"{get_api_base()}{path}", json=json_data, timeout=180)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        st.error(f"后端请求失败：{e}")
        return None


def sentiment_color(v: float | None) -> str:
    if v is None:
        return NEUTRAL
    if v > 0:
        return UP_COLOR
    if v < 0:
        return DOWN_COLOR
    return NEUTRAL


# ================= 页面：每日研判 =================
def page_dashboard():
    st.title("📊 每日市场研判")
    data = api_get("/api/v1/reports/today")
    if not data:
        st.info("暂无研判报告。请先在后端执行 `POST /api/v1/reports/generate`，或等待定时任务。")
        return

    c = data.get("content") or {}
    st.subheader(data.get("title", ""))
    st.caption(f"报告日期：{data.get('date', '')[:10]}  ·  模型：{data.get('model', '')}")

    # 数据时效：基于陈旧数据时必须明示，不能冒充"今日研判"
    fr = c.get("data_freshness") or {}
    if data.get("data_stale") or fr.get("stale"):
        cats = "、".join(fr.get("stale_categories") or []) or "部分类目"
        st.warning(
            f"⚠️ **数据时效提醒**：{cats} 类近期新闻不足，"
            f"本报告使用了**截至 {fr.get('stale_data_date') or '更早'}** 的数据（非当日）。"
        )

    # 运行环境降级：交易日历失效时会按"工作日"猜测，节假日可能被误判
    env = c.get("env") or {}
    if env.get("calendar_degraded"):
        st.error(
            "🔧 **运行环境降级**：交易日历不可用，系统正按「工作日」判断交易日。"
            "**节假日可能被误判为交易日**，本报告的生成时机可能不正确。"
        )
    if env.get("fallback"):
        st.error(
            "🔧 **多专家流程降级**：本次分析未能启用多专家协作，"
            "报告由单次生成产出，**可靠性低于常规报告**。"
        )

    # 专家缺席：失败的专家必须显式列出，否则报告会显得像全员参与
    failed = c.get("failed_experts") or []
    if failed:
        st.warning(f"⚠️ **专家缺席**：{'、'.join(failed)} 本次分析失败，未参与研判。")

    # 信息量标记：当天市场平静时如实说明，而不是硬凑内容
    if data.get("low_info"):
        info = c.get("info_level") or {}
        st.info(f"💤 **今日无重大消息**　（{info.get('reason', '四个信号均未触发')}）")

    c1, c2, c3 = st.columns(3)
    c1.metric("市场情绪", data.get("sentiment", "中性"))
    c2.metric("综合情绪分", f"{data.get('score', 0):+.2f}")
    c3.metric("置信度", data.get("confidence", ""))

    # —— 专家观点卡片 ——
    experts = data.get("expert_opinions") or []
    if experts:
        st.markdown("#### 专家观点")
        cols = st.columns(len(experts))
        for col, o in zip(cols, experts):
            with col:
                stance = o.get("stance", "中性")
                icon = {"看多": "🟥", "看空": "🟩"}.get(stance, "⬜")
                st.markdown(f"**{o.get('expert', '')}**")
                st.markdown(f"{icon} {stance} `{o.get('score', 0):+.2f}`")
                st.caption(f"置信度：{o.get('confidence', '')}")
                for p in (o.get("key_points") or [])[:2]:
                    st.caption(f"· {p[:55]}")

    # —— 风险官 ——
    risk = c.get("risk_opinion") or {}
    if risk:
        lvl = risk.get("risk_level", "中")
        emoji = {"高": "🔴", "中": "🟠", "低": "🟢"}.get(lvl, "⚪")
        with st.expander(f"{emoji} 风险官意见（风险等级：{lvl}）", expanded=(lvl == "高")):
            for r in risk.get("risks", []):
                st.markdown(f"- ⚠️ {r}")
            if risk.get("counter_arguments"):
                st.markdown("**🔍 反驳意见**")
                for x in risk["counter_arguments"]:
                    st.markdown(f"- {x}")
            if risk.get("worst_case"):
                st.markdown(f"**最坏情况**：{risk['worst_case']}")

    # —— 分歧度 ——
    div = data.get("divergence")
    if div is not None:
        desc = "高度一致" if div < 0.5 else ("严重分歧" if div > 1.0 else "存在分歧")
        veto = "　⚠️ 风险官已下调置信度" if data.get("risk_veto") else ""
        st.info(
            f"专家分歧度：**{div:.2f}**（{desc}）　最终置信度：**{data.get('confidence', '')}**{veto}"
        )

    st.markdown("#### 大盘综述")
    st.write(c.get("market_summary", ""))

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("#### 主要驱动因素")
        for x in c.get("key_drivers", []):
            st.markdown(f"- {x}")
        st.markdown("#### 板块机会")
        for x in c.get("sector_opportunities", []):
            st.markdown(f"- {x}")
    with col_b:
        st.markdown("#### 风险提示")
        for x in c.get("risks", []):
            st.markdown(f"- {x}")
        st.markdown("#### 引用新闻")
        for x in c.get("reference_news", []):
            st.markdown(f"- {x}")

    st.warning("⚠️ 以上内容由 AI 基于历史数据生成，仅供参考，不构成投资建议。")

    # 行情概览
    st.markdown("#### 主要指数概览")
    market = api_get("/api/v1/market", {"limit": 50})
    if market:
        latest = {}
        for r in market:
            latest.setdefault(r["symbol"], r)
        cols = st.columns(len(latest) or 1)
        for i, r in enumerate(latest.values()):
            pct = r.get("change_pct")
            cols[i].metric(
                r.get("name", ""),
                f"{r.get('close')}",
                f"{pct:+.2f}%" if pct is not None else "—",
            )


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
    st.subheader(c.get("market_summary", data.get("title", "")))
    st.caption(
        f"区间：{c.get('week_start', '')} ~ {c.get('week_end', '')}"
        f"　·　共 {c.get('daily_count', 0)} 份日报"
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("周定调", data.get("sentiment", "中性"))
    c2.metric("周综合分", f"{data.get('score', 0):+.2f}")
    div = data.get("divergence")
    c3.metric("日均分歧度", f"{div:.2f}" if div is not None else "—")

    # 日报定调走势（单序列，无需图例）
    series = c.get("daily_series") or []
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

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("#### 本周主线驱动")
        for x in c.get("key_drivers", []):
            st.markdown(f"- {x}")
        st.markdown("#### 板块机会")
        for x in c.get("sector_opportunities", []):
            st.markdown(f"- {x}")
    with col_b:
        st.markdown("#### 下周需警惕的风险")
        for x in c.get("risks", []):
            st.markdown(f"- {x}")

    if c.get("consensus_note"):
        st.markdown("#### 专家观点演变")
        st.write(c["consensus_note"])

    st.warning("⚠️ 以上内容由 AI 基于历史数据生成，仅供参考，不构成投资建议。")


# ================= 页面：新闻流 =================
def _fmt_sentiment(v):
    if v is None or pd.isna(v):
        return ""
    if v > 0:
        return f"🟥 {v:+.1f}"
    if v < 0:
        return f"🟩 {v:+.1f}"
    return f"⬜ {v:+.1f}"


def _fmt_themes(v):
    if not v:
        return ""
    try:
        themes = json.loads(v)
        if isinstance(themes, list):
            return " · ".join(str(t) for t in themes[:3])
    except Exception:
        pass
    return str(v)


def page_news():
    st.title("📰 金融新闻流")

    col1, col2 = st.columns([4, 1])
    keyword = col1.text_input("关键词（可空）", "")
    with col2:
        st.write("")
        if st.button("采集最新新闻", use_container_width=True):
            api_post("/api/v1/news/collect")
            st.rerun()

    data = api_get("/api/v1/news", {"limit": 1000})
    if not data:
        st.info("暂无新闻，点击「采集最新新闻」按钮。")
        return

    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["publish_time"]).dt.date

    # 日期标签按钮组
    dates = sorted(df["date"].unique(), reverse=True)
    date_map = {f"{d.month}月{d.day}日": d for d in dates}
    selected = st.pills("按日期筛选", ["全部日期"] + list(date_map.keys()), default="全部日期")

    col_a, col_b = st.columns([2, 1])
    days = col_a.slider("近 N 天", 1, 30, 7)
    limit = col_b.selectbox("显示条数", [100, 300, 500, 1000], index=1)

    if selected and selected != "全部日期":
        df = df[df["date"] == date_map[selected]]
    else:
        since = (pd.Timestamp.now() - pd.Timedelta(days=days)).date()
        df = df[df["date"] >= since]

    if keyword:
        df = df[df["title"].astype(str).str.contains(keyword, na=False)]

    df = df.head(limit)

    df["源数"] = df["source_count"].apply(
        lambda x: f"🔥 {int(x)}源" if x and x >= 2 else "1源"
    )
    df["情绪分"] = df["sentiment"].apply(_fmt_sentiment)
    df["题材"] = df["themes"].apply(_fmt_themes)
    df["时间"] = pd.to_datetime(df["publish_time"]).dt.strftime("%m-%d %H:%M")

    display = df[
        ["title", "category", "market", "源数", "情绪分", "题材", "时间", "url"]
    ].copy()
    display.columns = ["标题", "分类", "市场", "源数", "情绪分", "题材", "时间", "原文"]
    # 重置索引并生成从 1 开始的序号（筛选后重新编号）
    display = display.reset_index(drop=True)
    display.insert(0, "序号", range(1, len(display) + 1))

    st.dataframe(
        display,
        column_config={
            "原文": st.column_config.LinkColumn("原文", display_text="查看"),
        },
        hide_index=True,  # 隐藏 pandas 原生索引，只显示「序号」列
        use_container_width=True,
        height=600,
    )
    st.caption(f"共 {len(display)} 条新闻")

    s = df["sentiment"].dropna()
    if len(s):
        pos = int((s > 0).sum())
        neg = int((s < 0).sum())
        neu = int((s == 0).sum())
        st.markdown(f"情绪分布：🟥 偏多 {pos} ｜ 🟩 偏空 {neg} ｜ ⬜ 中性 {neu}")


# ================= 页面：行情 =================
def page_market():
    st.title("📉 指数行情")
    if st.button("采集最新行情"):
        api_post("/api/v1/market/collect")

    data = api_get("/api/v1/market", {"limit": 500})
    if not data:
        st.info("暂无行情数据，点击上方「采集最新行情」按钮。")
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
    latest = df.sort_values("date").groupby("symbol", as_index=False).last()
    table = latest[["name", "symbol", "close", "change_pct", "date"]]
    table.columns = ["名称", "代码", "收盘", "涨跌幅%", "日期"]
    st.dataframe(table, use_container_width=True)


# ================= 页面：AI 问答 =================
def page_chat():
    st.title("💬 AI 问答")
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    query = st.chat_input("问我关于市场/新闻的问题…")
    if query:
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

        # 构造历史（去掉当前 query 后的两两配对）
        history = []
        prev = st.session_state.messages[:-1]
        for i in range(0, len(prev) - 1, 2):
            if prev[i]["role"] == "user" and prev[i + 1]["role"] == "assistant":
                history.append([prev[i]["content"], prev[i + 1]["content"]])

        with st.chat_message("assistant"):
            with st.spinner("思考中…"):
                resp = api_post("/api/v1/agent/chat", {"query": query, "chat_history": history})
            answer = (resp or {}).get("answer", "（无响应）")
            st.markdown(answer)
        st.session_state.messages.append({"role": "assistant", "content": answer})


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
        st.caption("回测规则：综合情绪分 >0.1 视为看多、<-0.1 看空，与上证指数次日涨跌方向比对。仅供参考。")
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
        df = pd.DataFrame(reports)[["date", "sentiment", "score", "title"]]
        df.columns = ["日期", "情绪", "综合分", "标题"]
        st.dataframe(df, use_container_width=True, height=400)


# ================= 主入口 =================
def main():
    st.sidebar.title("📈 金融预测 Agent")
    api_base = st.sidebar.text_input("后端地址", value=get_api_base())
    st.session_state.api_base = api_base

    page = st.sidebar.radio(
        "导航",
        ["每日研判", "周度综述", "新闻流", "行情", "AI 问答", "历史与回测"],
    )
    if page == "每日研判":
        page_dashboard()
    elif page == "周度综述":
        page_weekly()
    elif page == "新闻流":
        page_news()
    elif page == "行情":
        page_market()
    elif page == "AI 问答":
        page_chat()
    else:
        page_history()


if __name__ == "__main__":
    main()
