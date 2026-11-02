"""页面：每日研判 (page_dashboard)。"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.api_client import api_get
from frontend.utils import _fmt_score


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

    if c.get("is_passed") is False:
        status = c.get("critic_status")
        if status == "rejected":
            st.error(
                f"🛑 **审计未通过：内容被驳回**，已修正 {c.get('retry_count', 0)} 次。"
                "审计发现具体问题，请查看批注。"
            )
        else:
            reason = {
                "error": "审计调用失败", "incomplete_sources": "原文证据不完整",
                "skipped": "缺少分析师底稿", "not_reviewed": "报告改写后尚未复审",
            }.get(status, "审计尚未完成")
            st.warning(f"⚠️ **尚未完成核验：{reason}**。这份报告尚无有效审计结论，请勿据此直接决策。")
        if c.get("critic_feedback"):
            with st.expander("查看审计批注"):
                st.write(c["critic_feedback"])
    elif c.get("is_passed") is True:
        st.caption(f"Critic 审计通过 · 修正 {c.get('retry_count', 0)} 次（模型核验结果）")

    # 数据时效：基于陈旧数据时必须明示，不能冒充"今日研判"
    fr = c.get("data_freshness") or {}
    if fr.get("evidence_excluded_count"):
        st.warning(
            f"⚠️ **新闻选取限制**：全文预算使 {fr['evidence_excluded_count']} 条候选新闻未纳入本次研判；"
            "分析范围仅涵盖已选取的全文，不能据此断言某事件未发生。"
        )
    if fr.get("coverage_incomplete"):
        st.warning(
            f"⚠️ **覆盖缺口提醒**：报告取数窗口仍有 {fr.get('pending_count', 0)} 条原文待处理、"
            f"{fr.get('open_gap_count', 0)} 个历史覆盖缺口。普通增量成功不代表历史已经补齐。"
        )
    if fr.get("round_overdue") or fr.get("collecting"):
        round_state = "采集仍在运行" if fr.get("collecting") else "最近采集未成功或已过期"
        st.warning(
            f"⚠️ **采集覆盖提醒**：按本报告分析时点（{fr.get('analyzed_at') or generated_at}），"
            f"{round_state}，**本窗口新闻尚未完整落地**。"
            f"上次成功：{fr.get('last_round_at') or '从未成功或未记录'}；"
            f"本报告新闻截至：{fr.get('newest_news_at') or fr.get('newest_news_date') or '未知'}。"
            "请勿将未收录的新闻理解为事件未发生。"
        )
    elif fr.get("readiness_scope") == "historical_unknown":
        st.info("历史补录报告：分析时点的采集执行状态未留存，覆盖完整性未知。")
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
        if (data.get("data_stale") or fr.get("stale") or fr.get("round_overdue")
                or fr.get("coverage_incomplete")
                or fr.get("evidence_excluded_count")
                or fr.get("collecting") or fr.get("readiness_scope") == "historical_unknown"):
            st.info("可用素材较少，采集覆盖尚不完整或未知，不能据此判断是否有重大消息。")
        else:
            st.info(f"💤 **今日无重大消息**　（{info.get('reason', '三个信号均未触发')}）")

    c1, c2, c3 = st.columns(3)
    c1.metric("市场情绪", data.get("sentiment", "中性"))
    c2.metric("综合情绪分", _fmt_score(data.get("score")))
    confidence = {"high": "高", "medium": "中", "low": "低"}.get(
        data.get("confidence"), data.get("confidence") or "—")
    c3.metric("置信度", confidence)

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
                expert_confidence = {"high": "高", "medium": "中", "low": "低"}.get(
                    o.get("confidence"), o.get("confidence") or "—")
                st.caption(f"置信度：{expert_confidence}")
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
            main_risks = c.get("risks") or []
            hidden = 0
            for r in risk.get("risks", []):
                if r in main_risks:
                    hidden += 1
                else:
                    st.markdown(f"- ⚠️ {r}")  # 历史主清单若遗漏，仍保留可见性
            if hidden:
                st.caption(f"{hidden} 条风险已纳入下方\u201c风险提示\u201d，此处不重复展示。")
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
            f"专家分歧度：**{div:.2f}**（{desc}）　最终置信度：**{confidence}**{veto}"
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
                    f"当日数据覆盖：{freshness.get('freshness') or ('陈旧' if freshness.get('stale') else '未知')}"
                )
                round_status = (
                    "采集中，本窗口新闻尚未完整落地" if freshness.get("collecting") else
                    "最近采集未成功或已过期" if freshness.get("round_overdue") else
                    "正常完成" if freshness.get("round_overdue") is False else "未知"
                )
                st.write(f"分析时点的采集轮状态：{round_status}")
                st.write(f"窗口内待处理原文：{freshness.get('pending_count', '未知')}　"
                         f"未解决历史缺口：{freshness.get('open_gap_count', '未知')}")
                st.write(
                    f"上次成功：{freshness.get('last_round_at') or '未记录'}　"
                    f"本报告新闻截至：{freshness.get('newest_news_at') or freshness.get('newest_news_date') or '未知'}"
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
