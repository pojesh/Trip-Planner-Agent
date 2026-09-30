"""Assistant panel: chat with the LangChain agent to refine the plan."""

import streamlit as st

import api_client as api
from api_client import ApiError
from ui.styles import esc

SUGGESTIONS = [
    "Make day 1 more relaxed",
    "Add a coffee break in the afternoon",
    "Swap a museum for something outdoors",
    "Which day has the most walking?",
]


def _md(text: str) -> str:
    return text.replace("$", "\\$")  # avoid accidental LaTeX rendering


def _send(trip: dict, text: str, box) -> None:
    ss = st.session_state
    with box:
        with st.chat_message("user", avatar="🙂"):
            st.markdown(_md(text))
    with st.status("Thinking…", expanded=False) as status:
        try:
            for ev in api.chat(trip["id"], text):
                if ev["type"] == "stage":
                    status.update(label=ev["label"])
                    st.markdown(f"• {esc(ev['label'])}", unsafe_allow_html=True)
                elif ev["type"] == "result":
                    ss.trip = ev["trip"]
                    if ev.get("changed"):
                        ss.flash = "Plan updated by the assistant"
                        ss.selected_stop, ss.focus = None, None
                    status.update(label="Done", state="complete")
                    st.rerun()
                elif ev["type"] == "error":
                    status.update(label="Something went wrong", state="error")
                    st.error(ev["message"])
                    return
        except ApiError as e:
            status.update(label="Something went wrong", state="error")
            st.error(e.message)


def render_chat(trip: dict, agent_on: bool) -> None:
    if not agent_on:
        st.info("The AI assistant needs a Gemini API key. Add `GEMINI_API_KEY=...` to `.env` and restart "
                "the backend. You can still edit the plan by hand in the Itinerary tab.")
        return

    box = st.container(height=520, border=False)
    with box:
        if not trip["chat"]:
            st.caption("Ask me to change anything about your plan.")
        for m in trip["chat"]:
            avatar = "🧭" if m["role"] == "assistant" else "🙂"
            with st.chat_message(m["role"], avatar=avatar):
                st.markdown(_md(m["content"]))
                if m.get("changed_plan"):
                    st.badge("Plan updated", icon=":material/check_circle:", color="green")

    last = trip["chat"][-1] if trip["chat"] else None
    replies = last.get("quick_replies") if last and last["role"] == "assistant" else []
    chips = replies or (SUGGESTIONS if len(trip["chat"]) <= 3 else [])
    picked = None
    if chips:
        st.markdown(f"<div class='qa-hint'>{'Quick replies' if replies else 'Try asking'}</div>",
                    unsafe_allow_html=True)
        with st.container(horizontal=True, gap="small"):
            for i, c in enumerate(chips):
                if st.button(c, key=f"chip_{i}_{len(trip['chat'])}", type="secondary"):
                    picked = c
    prompt = st.chat_input("Ask to change anything — e.g. “swap the museum for a park”", key="chat_in")
    text = picked or prompt
    if text:
        _send(trip, text, box)
