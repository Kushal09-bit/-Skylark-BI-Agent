"""
Streamlit front-end for the Skylark BI Agent. Every answer here comes from a
live Monday.com MCP read at the moment the question is asked — nothing is
cached across questions, and there is no static fallback data anywhere in
this app. See DECISION_LOG.md for the reasoning behind every non-obvious
choice (hosting, join keys, metric definitions, "leadership update" scope).
"""

import asyncio
import os
import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
load_dotenv()

from agent import ConversationState, ask  # noqa: E402
from leadership_update import generate_leadership_update  # noqa: E402
from llm_provider import resolve_provider  # noqa: E402

st.set_page_config(page_title="Skylark BI Agent", page_icon="📊", layout="centered")

MONDAY_TOKEN = os.environ.get("MONDAY_API_TOKEN")
LLM_PROVIDER = resolve_provider()
LLM_KEY_VAR = "GROQ_API_KEY" if LLM_PROVIDER == "groq" else "ANTHROPIC_API_KEY"
LLM_KEY = os.environ.get(LLM_KEY_VAR)

st.title("📊 Skylark BI Agent")
st.caption("Founder-level Q&A over your live Work Orders and Deals boards on Monday.com.")

if "conversation" not in st.session_state:
    st.session_state.conversation = ConversationState()
if "display_messages" not in st.session_state:
    st.session_state.display_messages = []

with st.sidebar:
    st.subheader("Connection")
    st.write("Monday.com token:", "✅ set" if MONDAY_TOKEN else "❌ missing")
    st.write(f"LLM provider ({LLM_PROVIDER}):", "✅ set" if LLM_KEY else f"❌ {LLM_KEY_VAR} missing")

    st.divider()
    st.subheader("Leadership update")
    period_days = st.number_input("Period (days)", min_value=1, max_value=365, value=30, step=1)
    generate_clicked = st.button("Generate leadership update", use_container_width=True)

    st.divider()
    if st.button("Reset conversation", use_container_width=True):
        st.session_state.conversation = ConversationState()
        st.session_state.display_messages = []
        st.rerun()

if not MONDAY_TOKEN or not LLM_KEY:
    missing = []
    if not MONDAY_TOKEN:
        missing.append("MONDAY_API_TOKEN")
    if not LLM_KEY:
        missing.append(LLM_KEY_VAR)
    st.error(
        f"Missing required configuration: {', '.join(missing)}. Set these as environment "
        f"variables (locally via `.env`, or as secrets on your hosting platform) before using the app. "
        f"See README.md for setup instructions."
    )
    st.stop()

for msg in st.session_state.display_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if generate_clicked:
    with st.chat_message("assistant"):
        with st.spinner("Pulling live data from both boards..."):
            update_text = asyncio.run(
                generate_leadership_update(MONDAY_TOKEN, period_days=int(period_days))
            )
        st.markdown(f"### 📋 Leadership Update — trailing {period_days} days\n\n{update_text}")
    st.session_state.display_messages.append({
        "role": "assistant",
        "content": f"### 📋 Leadership Update — trailing {period_days} days\n\n{update_text}",
    })

question = st.chat_input("Ask a question about your pipeline or delivery data...")
if question:
    st.session_state.display_messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Checking live Monday.com data..."):
            answer = asyncio.run(
                ask(question, st.session_state.conversation, MONDAY_TOKEN)
            )
        st.markdown(answer)
    st.session_state.display_messages.append({"role": "assistant", "content": answer})
