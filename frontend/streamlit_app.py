"""Streamlit 前端看板：每日研判 / 新闻流 / 行情 / AI 问答 / 历史回测。"""
from __future__ import annotations

import json
import logging
from datetime import date

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

# ---- 配色（中国金融惯例：红涨绿跌；同时用 +/- 符号做次要编码，不单靠颜色）----
# 色值经过 CVD（色盲）验证：红绿对是色盲最易混的组合，原先的 #d03b3b/#0ca30c
# 在 deuteranopia 下 ΔE 仅 4.1（几乎分不出）。改成下面这组后 ΔE = 27.0。
# 靠的是**拉开明度差**——色盲者分不出红绿，但分得出深浅。
# 校验方式：dataviz 技能的 scripts/validate_palette.js（light 模式全项通过）。
# ⚠️ 暗色模式需要另一套步进值；本项目所有图表目前都是单套配色，属于既有待办。
UP_COLOR = "#a32a2a"    # 涨（红，深）
DOWN_COLOR = "#6ec96e"  # 跌（绿，浅）
NEUTRAL = "#898781"     # 中性/文字
logger = logging.getLogger(__name__)

st.set_page_config(page_title="金融新闻情报简报 Agent", page_icon="📈", layout="wide")


# ================= 基础工具 =================
def get_api_base() -> str:
    return st.session_state.get("api_base", "http://localhost:8000")


@st.cache_resource
def _http_client() -> httpx.Client:
    return httpx.Client()


@st.cache_data(ttl=10, show_spinner=False)
def _cached_get(api_base: str, path: str, params_items: tuple):
    params = dict(params_items)
    r = _http_client().get(f"{api_base}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def api_get(path: str, params: dict | None = None):
    try:
        params_items = tuple(sorted((params or {}).items()))
        return _cached_get(get_api_base(), path, params_items)
    except Exception as e:  # noqa: BLE001
        logger.exception("GET 请求失败 path=%s", path)
        st.error("暂时无法获取数据，请检查后端服务后重试。")
        return None


def api_post(path: str, json_data: dict | None = None, *, invalidate_cache: bool = True):
    try:
        r = _http_client().post(f"{get_api_base()}{path}", json=json_data, timeout=180)
        r.raise_for_status()
        if invalidate_cache:
            _cached_get.clear()
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.exception("POST 请求失败 path=%s", path)
        st.error("操作暂时失败，请检查后端服务后重试。")
        return None


def _fmt_score(v) -> str:
    """分数格式化：**拿不到就显示占位符，绝不显示成 0**。

    `f"{None:+.2f}"` 会直接抛 `TypeError`（键存在、值为 None 时
    `data.get("score", 0)` 的默认值**不生效** —— 默认值只在键缺失时用）。

    ⚠️ 兜成 `0` 是**错的方向**：`0` 是「中性」这个**真实结论**，
    `None` 是「拿不到」。把后者显示成 `+0.00` 等于凭空造了一个结论
    —— 与本页 `divergence` 那行（`if div is not None else "—"`）保持一致。
    """
    return f"{v:+.2f}" if isinstance(v, (int, float)) else "—"


# ================= 页面：每日研判 =================
def page_dashboard(data: dict | None = None, *, include_market: bool = True):
    if data is None:
        st.title("📊 每日市场研判")
        data = api_get("/api/v1/reports/today")
    else:
        st.markdown("#### 日报详情")
    if not data:
        st.info("暂无研判报告。请先在后端执行 `POST /api/v1/reports/generate`，或等待定时任务。")
        return

    c = data.get("content") or {}
    title = data.get("title") or ""
    if not include_market and len(title) > 60:
        with st.expander("报告标题"):
            st.write(title)
    else:
        st.subheader(title)
    generated_at = str(data.get("date") or "")
    report_day = data.get("report_day") or generated_at[:10]
    st.caption(
        f"报告日期：{report_day}  ·  模型：{data.get('model', '')}"
    )
    if c.get("raw"):
        st.markdown("#### 报告正文")
        st.write(c["raw"])
        return

    # 数据时效：基于陈旧数据时必须明示，不能冒充"今日研判"
    fr = c.get("data_freshness") or {}
    if data.get("data_stale") or fr.get("stale"):
        cats = "、".join(fr.get("stale_categories") or [])
        if cats:
            st.warning(
                f"⚠️ **数据时效提醒**：{cats} 类近期新闻不足，"
                f"本报告使用了**截至 {fr.get('stale_data_date') or '更早'}** 的数据（非当日）。"
            )
        else:
            # H2（2026-09-23）：当日 0 条也会让 stale 为真，此时既没有「哪些类目不足」
            # 也没有「截至哪天」—— 直接说 0 条，别拼出「部分类目」这种话。
            st.warning(
                f"⚠️ **数据时效提醒**：**当日（{fr.get('today')}）新闻 0 条**。"
                "快讯是 7×24 的，这通常不是「今天没消息」，而是**当日的新闻没有进来**。"
                "本次研判使用的是更早的新闻，请勿把「未出现」读成「未发生」。"
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
        st.info(f"💤 **今日无重大消息**　（{info.get('reason', '三个信号均未触发')}）")

    c1, c2, c3 = st.columns(3)
    c1.metric("市场情绪", data.get("sentiment", "中性"))
    c2.metric("综合情绪分", _fmt_score(data.get("score")))
    c3.metric("置信度", data.get("confidence", ""))

    # —— 专家观点 ——
    # 顶部使用可展开区域，保留完整观点、证据和不确定性，避免窄屏多列压缩。
    experts = data.get("expert_opinions") or []
    if experts:
        st.markdown("#### 专家观点")
        for o in experts:
            if not isinstance(o, dict):
                continue
            stance = o.get("stance", "中性")
            icon = {"看多": "🟥", "看空": "🟩"}.get(stance, "⬜")
            label = (
                f"{icon} {o.get('expert', '未命名专家')} · {stance} · "
                f"综合分 {_fmt_score(o.get('score'))}"
            )
            with st.expander(label):
                st.caption(f"置信度：{o.get('confidence', '')}")
                key_points = o.get("key_points") or []
                if not isinstance(key_points, list):
                    key_points = []
                if key_points:
                    st.markdown("**核心观点**")
                    for point in key_points:
                        st.markdown(f"- {point}")
                evidence = o.get("evidence") or []
                if not isinstance(evidence, list):
                    evidence = []
                if evidence:
                    st.markdown("**证据**")
                    for item in evidence:
                        st.markdown(f"- {item}")
                uncertainties = o.get("uncertainties") or []
                if not isinstance(uncertainties, list):
                    uncertainties = []
                if uncertainties:
                    st.markdown("**不确定性**")
                    for item in uncertainties:
                        st.markdown(f"- {item}")

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

    consensus_note = c.get("consensus_note")
    if consensus_note:
        with st.expander("专家共识与主要分歧"):
            st.write(consensus_note)

    info = c.get("info_level") or {}
    freshness = c.get("data_freshness") or {}
    if info or freshness:
        with st.expander("信息量与数据质量"):
            if info.get("reason"):
                st.write(f"信息量判断：{info['reason']}")
            signals = info.get("signals") or {}
            if isinstance(signals, dict) and signals:
                signal_rows = []
                for signal in signals.values():
                    if isinstance(signal, dict):
                        signal_rows.append(signal)
                if signal_rows:
                    st.dataframe(pd.DataFrame(signal_rows), use_container_width=True)
            if freshness:
                st.write(
                    f"最新新闻日期：{freshness.get('newest_news_date') or '未知'}　"
                    f"数据状态：{freshness.get('freshness') or ('陈旧' if freshness.get('stale') else '正常')}"
                )

    st.markdown("#### 大盘综述")
    st.write(c.get("market_summary", ""))

    for label, key in (
        ("主要驱动因素", "key_drivers"),
        ("板块机会", "sector_opportunities"),
        ("风险提示", "risks"),
        ("引用新闻", "reference_news"),
    ):
        items = c.get(key) or []
        with st.expander(f"{label}（{len(items)}）"):
            for x in items:
                st.markdown(f"- {x}")

    st.warning("⚠️ 以上内容由 AI 基于历史数据生成，仅供参考，不构成投资建议。")

    if include_market:
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
    c2.metric("周综合分", _fmt_score(data.get("score")))
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
    # 跨年时标签必须带年份，否则「9月5日」会撞键、导致某一年那一天点不到
    _multi_year = dates[0].year != dates[-1].year
    date_map: dict = {}
    date_count: dict = {}
    for r in date_rows:
        d = date.fromisoformat(r["date"])
        label = d.isoformat() if _multi_year else f"{d.month}月{d.day}日"
        date_map[label] = d
        date_count[label] = r["count"]
    selected = st.pills("按日期筛选", ["全部日期"] + list(date_map.keys()), default="全部日期")

    # 数据可用范围必须显式说明：下面的滑块能拉到 30 天，但库里未必有那么多天。
    # 标签组只列「有数据的日期」，空缺是**静默跳过**的（比如 09-18 直接跳到 09-16）。
    _span = (dates[0] - dates[-1]).days + 1
    _missing = _span - len(dates)
    _suffix = f"，其中 **{_missing} 天无数据**）" if _missing > 0 else "）"
    st.caption(
        f"📅 库中新闻覆盖 **{dates[-1]} ~ {dates[0]}**"
        f"（共 {len(dates)} 天有数据" + _suffix
    )

    col_a, col_b = st.columns([2, 1])
    days = col_a.slider("近 N 天", 1, 30, 7)
    limit = col_b.selectbox("显示条数", [100, 300, 500, 1000], index=1)

    # ② 按需向后端取，筛选交给数据库做 —— 不再拿一坨固定数据在本地硬筛。
    #    选具体某天就只要那天；否则按「近 N 天」窗口。
    params: dict = {"limit": limit}
    if selected and selected != "全部日期":
        d = date_map[selected]
        params["start"] = d.isoformat()
        params["end"] = d.isoformat()
    else:
        params["days"] = days
    if keyword:
        params["keyword"] = keyword

    data = api_get("/api/v1/news", params)
    if not data:
        st.info(
            "该筛选条件下**没有新闻数据**。\n\n"
            "常见原因：① 选定日期当天没有采集到新闻；"
            "② 「近 N 天」窗口内库里没有数据；③ 关键词没有命中。"
        )
        return

    df = pd.DataFrame(data)

    # 单日条数可能超过「显示条数」而被服务端截断 —— 不说明会让人以为那天只有这么多
    if selected and selected != "全部日期":
        _total = date_count.get(selected, 0)
        if _total > len(df):
            st.caption(
                f"⚠️ 该日共 **{_total}** 条，此处按「显示条数」只展示最新的 "
                f"**{len(df)}** 条（调大「显示条数」可看更多）。"
            )

    df["源数"] = df["source_count"].apply(
        lambda x: f"🔥 {int(x)}源" if x and x >= 2 else "1源"
    )
    df["题材"] = df["themes"].apply(_fmt_themes)
    df["时间"] = pd.to_datetime(df["publish_time"]).dt.strftime("%m-%d %H:%M")

    display = df[
        ["title", "category", "market", "源数", "题材", "时间", "url"]
    ].copy()
    display.columns = ["标题", "分类", "市场", "源数", "题材", "时间", "原文"]
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
        pending_user = None
        for message in st.session_state.messages[:-1]:
            if message.get("role") == "user":
                pending_user = message.get("content")
            elif message.get("role") == "assistant" and pending_user is not None:
                history.append([pending_user, message.get("content", "")])
                pending_user = None

        with st.chat_message("assistant"):
            with st.spinner("思考中…"):
                resp = api_post(
                    "/api/v1/agent/chat",
                    {"query": query, "chat_history": history},
                    invalidate_cache=False,
                )
            answer = (resp or {}).get("answer") if resp else None
            if answer:
                st.markdown(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})
            else:
                st.error("本次问答没有返回结果，请稍后重试。")


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


# ================= 主入口 =================
def main():
    st.sidebar.title("📈 金融情报简报 Agent")
    api_base = st.sidebar.text_input("后端地址", value=get_api_base())
    st.session_state.api_base = api_base
    if st.sidebar.button("刷新数据"):
        _cached_get.clear()
        st.rerun()

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
