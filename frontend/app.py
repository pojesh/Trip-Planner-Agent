"""Streamlit entry point: `streamlit run frontend/app.py` (from the repo root)."""

import streamlit as st

st.set_page_config(page_title="Wayfinder · AI Trip Planner", page_icon="🧭", layout="wide",
                   initial_sidebar_state="expanded")

import api_client as api  # noqa: E402
from api_client import ApiError  # noqa: E402
from ui.forms import render_planner  # noqa: E402
from ui.itinerary import render_trip  # noqa: E402
from ui.styles import esc, fmt_date, inject_css  # noqa: E402

inject_css()
ss = st.session_state
ss.setdefault("trip", None)


@st.cache_data(ttl=20, show_spinner=False)
def backend_health():
    try:
        return api.health()
    except ApiError:
        return None


def new_trip():
    for key in ("trip", "dest", "dest_results", "origin", "origin_results", "pending_q", "gen_payload",
                "selected_stop", "focus", "last_pick"):
        ss[key] = None
    ss.selected_day = 1


def open_trip(trip_id: str):
    try:
        ss.trip = api.get_trip(trip_id)
        ss.selected_day, ss.selected_stop, ss.focus, ss.last_pick = 1, None, None, None
    except ApiError as e:
        ss.flash_error = e.message


def delete_trip(trip_id: str):
    try:
        api.delete_trip(trip_id)
        if ss.trip and ss.trip["id"] == trip_id:
            new_trip()
        ss.flash = "Trip deleted"
    except ApiError as e:
        ss.flash_error = e.message


def sidebar(health):
    with st.sidebar:
        st.markdown('<div class="brand"><div class="brand-logo">🧭</div><div><div class="brand-name">Wayfinder</div>'
                    '<div class="brand-tag">AI trip planner on open map data</div></div></div>',
                    unsafe_allow_html=True)
        st.button("New trip", icon=":material/add:", type="primary", width="stretch", on_click=new_trip)

        st.markdown('<div class="side-label">My trips</div>', unsafe_allow_html=True)
        try:
            trips = api.list_trips() if health else []
        except ApiError:
            trips = []
        if not trips:
            st.caption("Saved trips appear here.")
        for t in trips:
            with st.container(key=f"tripitem_{t['id']}"):
                st.markdown(f"<div class='trip-title'>{esc(t['title'])}</div>"
                            f"<div class='trip-meta'>{esc(t['destination_label'].split(',')[0])} · "
                            f"{fmt_date(t['start_date'])}</div>", unsafe_allow_html=True)
                with st.container(horizontal=True, gap="small"):
                    st.button("Open", key=f"open_{t['id']}", on_click=open_trip, args=(t["id"],), type="tertiary")
                    with st.popover("Delete", type="tertiary"):
                        st.caption("Delete this trip permanently?")
                        st.button("Yes, delete", key=f"del_{t['id']}", type="primary",
                                  on_click=delete_trip, args=(t["id"],))

        st.markdown('<div class="side-label">Status</div>', unsafe_allow_html=True)
        if health is None:
            st.markdown("<span class='status-dot' style='background:#EF4444'></span>Backend offline",
                        unsafe_allow_html=True)
        elif health.get("agent") == "gemini":
            st.markdown(f"<span class='status-dot' style='background:#22C55E'></span>AI agent · "
                        f"{esc(health.get('model'))}", unsafe_allow_html=True)
        else:
            st.markdown("<span class='status-dot' style='background:#F59E0B'></span>Built-in planner "
                        "(no Gemini key)", unsafe_allow_html=True)
        st.caption("Map data © OpenStreetMap contributors · Routing by OSRM")


health = backend_health()
sidebar(health)

msg = ss.get("flash")
if msg:
    st.toast(msg, icon="✅")
    ss.flash = None
err = ss.get("flash_error")
if err:
    st.toast(err, icon="⚠️")
    ss.flash_error = None

if health is None:
    st.error("Can't reach the planner backend. Start everything with `python run.py` "
             "(or run `uvicorn backend.main:app` from the project folder), then refresh.")
    st.stop()

if ss.trip:
    render_trip(agent_on=health.get("agent") == "gemini")
else:
    if health.get("agent") != "gemini":
        st.info("Running without an AI key — trips are planned by the built-in planner. "
                "Add `GEMINI_API_KEY` to `.env` and restart to enable the LangChain agent and chat.", icon="💡")
    render_planner()
