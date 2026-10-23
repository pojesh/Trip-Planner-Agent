"""Help & bookings page: the support assistant chat and the Dev Board."""

import streamlit as st

from ui.support import render_support

render_support(agent_on=st.session_state.health.get("agent") == "ai")
