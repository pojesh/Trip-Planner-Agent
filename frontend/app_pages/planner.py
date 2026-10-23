"""Planner page: the trip form, or the open trip's map and itinerary."""

import streamlit as st

from ui.forms import render_planner
from ui.itinerary import render_trip

ss = st.session_state
health = ss.health

if ss.trip:
    render_trip(agent_on=health.get("agent") == "ai")
else:
    if health.get("agent") != "ai":
        st.info("Running without an AI key — trips are planned by the built-in planner. "
                f"Add `{health.get('key_name', 'GEMINI_API_KEY')}` to `.env` (provider set by `LLM_PROVIDER`) "
                "and restart to enable the LangChain agent and chat.", icon="💡")
    render_planner()
