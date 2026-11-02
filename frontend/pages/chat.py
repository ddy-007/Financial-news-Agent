"""页面：AI 问答 (page_chat)。"""
from __future__ import annotations

import streamlit as st

from frontend.api_client import api_post


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
