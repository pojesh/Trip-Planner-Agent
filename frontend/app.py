"""Streamlit entry point: `streamlit run frontend/app.py` (from the repo root)."""

import streamlit as st

st.set_page_config(page_title="Wayfinder · AI Trip Planner", page_icon="🧭", layout="wide",
                   initial_sidebar_state="expanded")

import api_client as api  # noqa: E402
from api_client import ApiError  # noqa: E402
from ui.styles import esc, fmt_date, inject_css  # noqa: E402

inject_css()
ss = st.session_state
ss.setdefault("trip", None)
ss.setdefault("goto", None)  # "planner" | "support": switch page on the next run


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
    ss.goto = "planner"


def open_trip(trip_id: str):
    try:
        ss.trip = api.get_trip(trip_id)
        ss.selected_day, ss.selected_stop, ss.focus, ss.last_pick = 1, None, None, None
        ss.goto = "planner"
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


BOOKING_LABEL = {"booked": "🎫 Booked", "cancelled": "Cancelled"}


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
            booking = BOOKING_LABEL.get(t.get("booking_status"), "")
            with st.container(key=f"tripitem_{t['id']}"):
                st.markdown(f"<div class='trip-title'>{esc(t['title'])}</div>"
                            f"<div class='trip-meta'>{esc(t['destination_label'].split(',')[0])} · "
                            f"{fmt_date(t['start_date'])}{' · ' + booking if booking else ''}</div>",
                            unsafe_allow_html=True)
                with st.container(horizontal=True, gap="small"):
                    st.button("Open", key=f"open_{t['id']}", on_click=open_trip, args=(t["id"],), type="tertiary")
                    with st.popover("Delete", type="tertiary"):
                        if t.get("booking_status") == "booked":
                            st.caption("This trip is booked. Cancel the booking in Help & bookings first.")
                        else:
                            st.caption("Delete this trip permanently?")
                            st.button("Yes, delete", key=f"del_{t['id']}", type="primary",
                                      on_click=delete_trip, args=(t["id"],))

        st.markdown('<div class="side-label">Status</div>', unsafe_allow_html=True)
        if health is None:
            st.markdown("<span class='status-dot' style='background:#EF4444'></span>Backend offline",
                        unsafe_allow_html=True)
        elif health.get("agent") == "ai":
            st.markdown(f"<span class='status-dot' style='background:#22C55E'></span>AI agent · "
                        f"{esc(str(health.get('provider', '')).capitalize())} · {esc(health.get('model'))}",
                        unsafe_allow_html=True)
        else:
            st.markdown("<span class='status-dot' style='background:#F59E0B'></span>Built-in planner "
                        f"(no {esc(health.get('key_name', 'LLM key'))})", unsafe_allow_html=True)
        st.caption("Map data © OpenStreetMap contributors · Routing by OSRM")


planner_page = st.Page("app_pages/planner.py", title="Trip planner", icon=":material/map:", default=True)
support_page = st.Page("app_pages/support.py", title="Help & bookings", icon=":material/support_agent:")
page = st.navigation([planner_page, support_page], position="top")

health = backend_health()
ss.health = health
sidebar(health)

goto = ss.goto
ss.goto = None
target = {"planner": planner_page, "support": support_page}.get(goto)
if target is not None and page.url_path != target.url_path:
    st.switch_page(target)

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

page.run()
