"""Streamlit chat UI on the same backend as the CLI and the eval runner."""
from __future__ import annotations

import streamlit as st

from app.assistant import answer_question, warm_up

st.set_page_config(page_title="RiverPay Assistant", page_icon="\U0001F4AC")
st.title("RiverPay customer assistant")
st.caption(
    "Answers only from the RiverPay knowledge pack. Balances, loan approvals, FX rates and attempts to "
    "override policy are refused; account-specific problems are handed to a human (mocked)."
)

with st.spinner("Loading knowledge pack..."):
    warm_up()

if "turns" not in st.session_state:
    st.session_state.turns = []
show_debug = st.sidebar.checkbox("Show retrieval debug (notes)")


def render(result: dict) -> None:
    st.write(result["answer"])
    tags = [t for t, on in (("REFUSAL", result["refusal"]), ("HANDOFF TO HUMAN", result["handoff_to_human"])) if on]
    if tags:
        st.caption(" · ".join(tags))
    if result["citations"]:
        st.caption("Sources: " + "; ".join(f"{c['doc']} › {c['section']}" for c in result["citations"]))
    if show_debug:
        st.code(result["notes"], language=None)


for question, result in st.session_state.turns:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        render(result)

prompt = st.chat_input("Ask about fees, limits, PIN reset, transfers...")
if prompt:
    with st.chat_message("user"):
        st.write(prompt)
    history = [(q, r["answer"]) for q, r in st.session_state.turns]
    with st.chat_message("assistant"), st.spinner("Checking the handbook..."):
        result = answer_question(None, prompt, history)
        render(result)
    st.session_state.turns.append((prompt, result))
